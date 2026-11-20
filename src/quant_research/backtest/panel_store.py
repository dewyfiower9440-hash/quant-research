"""Panel store and point-in-time universe panels (v2.2 B3, design delta D7).

A research panel over a whole research window (36 or 19 months) and a few hundred symbols is too slow to
rebuild from 1m lake files for every campaign cell. The store builds one shard per symbol with the ordinary
`load_panel` (so every point-in-time and seal rule applies unchanged) and caches it as float32 parquet:

- rows: the shard is the symbol's rows of the full window panel, cut only before the first bar exists and after
  the last executable interval. Rows between membership spans are KEPT: features are row-based (`shift`,
  `rolling`, `ewm`) and assume one row per bar, so the shard equals `load_panel` on every row it keeps.
- the key is a sha256 of everything that defines the content: symbol, window, panel config, rule, role, the
  symbol's membership spans, each dataset's time semantics, effective and latest lag, the seal windows, the lake
  files (path, size) and the source of the modules that build it. A regenerated lake file, a re-calibrated lag,
  a changed membership or seal, or a code change makes a new shard; old shards stay on disk until removed.
- prices, volumes and rates are stored as float32 (relative error ~6e-8, below any cost) and read back as float64;
  timestamps and counts stay Int64.

`load_universe_panel` assembles the shards of a window into one `Panel`. With `top_m` the tradable universe of a
month is the top-M members (membership at the month's start) by the quote volume of the PREVIOUS calendar month
that was available at the month's first decision, inside the same window: the first month of a window is
warm-up only (its previous month may lie in a sealed holdout) and the panel's `evaluate_from_ns` says so."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import cast

import polars as pl

from quant_research.backtest import panel as panel_module
from quant_research.backtest.panel import Panel, PanelConfig, load_panel, membership_spans, months_between
from quant_research.data import access as access_module
from quant_research.data import lake as lake_module
from quant_research.data.access import PointInTime
from quant_research.data.lake import bulk_files, read_derived_file
from quant_research.errors import AppError
from quant_research.strategy.ops import FUNDING, KLINES, MARK, PREMIUM

STORE_VERSION = 2
DEFAULT_DATASETS = (KLINES, MARK, PREMIUM, FUNDING)


class ShardError(AppError):
    """A shard could not be built or read consistently."""


def _source_hash() -> str:
    """The code that defines a shard's content: a change anywhere in it invalidates every shard."""
    h = hashlib.sha256()
    for module_file in (panel_module.__file__, access_module.__file__, lake_module.__file__, __file__):
        h.update(Path(str(module_file)).read_bytes())
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class Window:
    """[first_ns, end_ns): the bars a panel may read; for research it must lie inside one research window."""

    first_ns: int
    end_ns: int

    def __post_init__(self) -> None:
        if self.end_ns <= self.first_ns:
            raise ValueError(f"empty window [{self.first_ns}, {self.end_ns})")

    def months(self) -> list[str]:
        return months_between(self.first_ns, self.end_ns - 10**9)  # 1 s before: ns - 1 rounds back up in float

    def last_bar_ns(self, config: PanelConfig) -> int:
        """The last bar whose decision (bar end + delay) still lies before `end_ns`."""
        return self.end_ns - 2 * config.timeframe_ns


@dataclass(frozen=True, slots=True)
class ShardMeta:
    key: str
    symbol: str
    files: tuple[tuple[str, int], ...]  # lake files read (path, size): the data version
    rank_qv: dict[str, float]  # month m-1 -> its quote volume available at month m's first decision
    rows: int
    member_rows: int


@dataclass(frozen=True, slots=True)
class UniversePanel:
    panel: Panel
    built: int  # shards built in this call
    reused: int  # shards read from the cache
    missing: tuple[str, ...]  # members without kline lake files in the window


def _month(ns_col: str) -> pl.Expr:
    return pl.from_epoch(pl.col(ns_col), time_unit="ns").dt.strftime("%Y-%m")


def _month_start_ns(month: str) -> int:
    return int(datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC).timestamp()) * 10**9


def window_symbols(pit: PointInTime, rule: str, window: Window) -> list[str]:
    """Symbols with a membership span of `rule` overlapping the window (point-in-time membership)."""
    spans = membership_spans(pit, rule)
    if spans is None:
        return []
    hit = spans.filter(
        (pl.col("from_ns") < window.end_ns) & (pl.col("to_ns").is_null() | (pl.col("to_ns") > window.first_ns))
    )
    return sorted(set(hit["symbol"].to_list()))


def members_by_month(pit: PointInTime, rule: str, window: Window) -> dict[str, set[str]]:
    """Per calendar month of the window: the symbols that are members at the month's start (the ranking is
    made then, so a span starting later in the month is not yet known)."""
    spans = membership_spans(pit, rule)
    out: dict[str, set[str]] = {m: set() for m in window.months()}
    if spans is None:
        return out
    for month in out:
        start = max(_month_start_ns(month), window.first_ns)
        hit = spans.filter((pl.col("from_ns") <= start) & (pl.col("to_ns").is_null() | (pl.col("to_ns") > start)))
        out[month] = set(hit["symbol"].to_list())
    return out


def top_m_by_month(
    qv: Mapping[str, Mapping[str, float]], members: Mapping[str, set[str]], months: Sequence[str], *, m: int
) -> dict[str, set[str]]:
    """Pure ranking: month k's universe = the m members of month k with the largest quote volume in month k-1
    (ties by symbol). The first month has no earlier month inside the window and gets an empty universe."""
    if m < 1:
        raise ValueError(f"top m must be >= 1, got m={m}")
    out: dict[str, set[str]] = {}
    for k, month in enumerate(months):
        if k == 0:
            out[month] = set()
            continue
        prev = months[k - 1]
        ranked = sorted(
            (s for s in members.get(month, set()) if qv.get(s, {}).get(prev, 0.0) > 0.0),
            key=lambda s: (-qv[s][prev], s),
        )
        out[month] = set(ranked[:m])
    return out


def trim_to_data(frame: pl.DataFrame) -> pl.DataFrame:
    """Per symbol, the rows from the first decision that sees a bar to the last executable interval; nothing in
    between is dropped (row-based features need one row per bar)."""
    asof = [c for c in ("bar_start_ns", "mark_close", "premium_close", "funding_rate") if c in frame.columns]
    any_seen = pl.any_horizontal([pl.col(c).is_not_null() for c in asof])  # any as-of input already visible
    last_bar = pl.col("bar_start_ns").max().over("symbol")
    marked = frame.sort(["symbol", "decision_ns"]).with_columns(
        any_seen.cast(pl.Int64).cum_sum().over("symbol").alias("_seen"),
        (pl.col("exec_minutes") > 0).cast(pl.Int64).reverse().cum_sum().reverse().over("symbol").alias("_left"),
        # the first decision that sees the symbol's last bar is kept too: L0 marks an open position to it
        (pl.col("bar_start_ns") == last_bar).fill_null(False).cast(pl.Int64).cum_sum().over("symbol").alias("_last"),
    )
    keep = (pl.col("_seen") > 0) & ((pl.col("_left") > 0) | (pl.col("_last") <= 1))
    return marked.filter(keep).drop("_seen", "_left", "_last")


def rank_volumes(frame: pl.DataFrame, months: Sequence[str]) -> dict[str, float]:
    """For each month m after the first: the quote volume of month m-1's bars that the decision at the start of
    month m could see (its as-of bar and every earlier bar), keyed by m-1. One row per bar is counted once."""
    if frame.is_empty():
        return {}
    bars = frame.drop_nulls("bar_start_ns").unique(subset=["bar_start_ns"], keep="first")
    bars = bars.select("bar_start_ns", pl.col("quote_volume").fill_null(0.0), _month("bar_start_ns").alias("_m"))
    decisions = frame.sort("decision_ns").with_columns(_month("decision_ns").alias("_m"))
    out: dict[str, float] = {}
    for prev, month in pairwise(months):
        first = decisions.filter(pl.col("_m") == month).head(1)
        if first.is_empty() or first["bar_start_ns"][0] is None:
            continue
        cutoff = int(first["bar_start_ns"][0])
        seen = bars.filter((pl.col("_m") == prev) & (pl.col("bar_start_ns") <= cutoff))
        if seen.height:
            out[prev] = float(seen["quote_volume"].sum())
    return out


def _to_float32(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(pl.col(pl.Float64).cast(pl.Float32))


def _to_float64(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(pl.col(pl.Float32).cast(pl.Float64))


def _tmp(target: Path) -> Path:
    return target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")  # unique per writer


def _write_atomic(frame: pl.DataFrame, target: Path) -> None:
    tmp = _tmp(target)
    frame.write_parquet(tmp)
    os.replace(tmp, target)


@dataclass(frozen=True, slots=True)
class PanelStore:
    root: Path

    def key(
        self,
        pit: PointInTime,
        data_root: Path,
        symbol: str,
        window: Window,
        config: PanelConfig,
        *,
        rule: str | None,
        datasets: Sequence[str],
    ) -> tuple[str, tuple[tuple[str, int], ...]]:
        months = months_between(window.first_ns, window.last_bar_ns(config) + config.timeframe_ns + panel_module.DAY_NS)
        files: list[tuple[str, int]] = []
        present: list[str] = []  # datasets with files: only these are read, so only their lag / seal matter
        for dataset_id in datasets:
            found = [
                (f.relative_to(data_root).as_posix(), f.stat().st_size)
                for f in bulk_files(data_root, dataset_id, [symbol], months)
            ]
            files += found
            if found:
                present.append(dataset_id)
        spans = membership_spans(pit, rule)
        own = [] if spans is None else sorted(spans.filter(pl.col("symbol") == symbol).iter_rows())
        lags: dict[str, object] = {}
        for ds in present:
            row = pit.catalog.conn.execute(
                "select time_semantics, coalesce(interval_ms, 0) from dataset where dataset_id = ?", (ds,)
            ).fetchone()
            pinned = pit.lag_versions.get(ds)
            lags[ds] = {
                "semantics": None if row is None else list(row),
                "pinned": pinned,
                "effective": pit.catalog.lag_ns(ds, pinned),
                "latest": pit.catalog.lag_ns(ds),
            }
        identity = {
            "store_version": STORE_VERSION,
            "source": _source_hash(),
            "polars": pl.__version__,
            "symbol": symbol,
            "window": [window.first_ns, window.end_ns],
            "config": [config.timeframe_ns, config.decision_delay_ns, config.latency_ns],
            "rule": rule,
            "role": pit.role,
            "reproduce_archived_run": pit.reproduce_archived_run,
            "datasets": list(datasets),
            "lags": lags,
            "seal": {ds: pit.catalog.research_windows(ds) for ds in present},
            "membership": own,
            "files": files,
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        return digest, tuple(files)

    def shard(
        self,
        pit: PointInTime,
        data_root: Path,
        symbol: str,
        window: Window,
        config: PanelConfig,
        *,
        rule: str | None,
        datasets: Sequence[str] = DEFAULT_DATASETS,
    ) -> tuple[ShardMeta, bool]:
        """The cached shard (and False) or a freshly built one (and True). Building runs `load_panel`, so a
        research-role window that is not inside one research window raises `SealViolationError` and caches
        nothing; a symbol without kline files raises `FileNotFoundError`."""
        key, files = self.key(pit, data_root, symbol, window, config, rule=rule, datasets=datasets)
        meta_path = self.root / f"{key}.json"
        if meta_path.exists():
            return self._meta(meta_path), False
        panel = load_panel(
            pit,
            data_root,
            [symbol],
            first_bar_ns=window.first_ns,
            last_bar_ns=window.last_bar_ns(config),
            config=config,
            rule=rule,
            datasets=datasets,
        )
        if sorted(panel.files) != sorted(files):
            raise ShardError(f"lake files of {symbol} changed while its shard was built; rerun to rebuild")
        frame = trim_to_data(panel.frame)
        member_rows = int(frame["member"].sum()) if frame.height else 0
        meta = ShardMeta(key, symbol, files, rank_volumes(panel.frame, window.months()), frame.height, member_rows)
        self.root.mkdir(parents=True, exist_ok=True)
        _write_atomic(_to_float32(frame), self.root / f"{key}.parquet")
        _write_atomic(_to_float32(panel.funding), self.root / f"{key}.funding.parquet")
        payload = {
            "key": key,
            "symbol": symbol,
            "files": files,
            "rank_qv": meta.rank_qv,
            "rows": meta.rows,
            "member_rows": member_rows,
        }
        tmp = _tmp(meta_path)
        tmp.write_bytes(json.dumps(payload, sort_keys=True).encode("utf-8"))
        os.replace(tmp, meta_path)  # the meta file is written last: its presence means the shard is complete
        return meta, True

    def _meta(self, path: Path) -> ShardMeta:
        raw = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        files = tuple((str(p), int(s)) for p, s in cast(list[list[object]], raw["files"]) if isinstance(s, int))
        qv = {str(k): float(v) for k, v in cast(dict[str, float], raw["rank_qv"]).items()}
        rows, member_rows = raw["rows"], raw["member_rows"]
        if not isinstance(rows, int) or not isinstance(member_rows, int):
            raise ShardError(f"shard meta {path} has no integer row counts")
        return ShardMeta(str(raw["key"]), str(raw["symbol"]), files, qv, rows, member_rows)

    def read(self, meta: ShardMeta) -> tuple[pl.DataFrame, pl.DataFrame]:
        """(panel rows, funding settlements) of a shard, floats back to float64; the row count must match."""
        frame = _to_float64(read_derived_file(self.root / f"{meta.key}.parquet"))
        if frame.height != meta.rows:
            raise ShardError(f"shard {meta.key} of {meta.symbol} has {frame.height} rows, its meta {meta.rows}")
        funding = _to_float64(read_derived_file(self.root / f"{meta.key}.funding.parquet"))
        return frame, funding


def load_universe_panel(
    pit: PointInTime,
    data_root: Path,
    store: PanelStore,
    window: Window,
    config: PanelConfig,
    *,
    top_m: int | None,
    rule: str,
    datasets: Sequence[str] = DEFAULT_DATASETS,
    symbols: Sequence[str] | None = None,
) -> UniversePanel:
    """One panel over the window from cached shards. `top_m=None`: the rule's members as they are; otherwise
    member = rule member AND in that month's top-M by the previous month's quote volume (`top_m_by_month`)."""
    wanted = list(symbols) if symbols is not None else window_symbols(pit, rule, window)
    metas: dict[str, ShardMeta] = {}
    built = reused = 0
    missing: list[str] = []
    for symbol in wanted:
        try:
            meta, fresh = store.shard(pit, data_root, symbol, window, config, rule=rule, datasets=datasets)
        except FileNotFoundError:
            missing.append(symbol)  # a member without downloaded klines: reported, not silently dropped
            continue
        built, reused = built + fresh, reused + (not fresh)
        if meta.member_rows:
            metas[symbol] = meta
    months = window.months()
    top: dict[str, set[str]] | None = None
    if top_m is not None:
        qv = {s: m.rank_qv for s, m in metas.items()}
        top = top_m_by_month(qv, members_by_month(pit, rule, window), months, m=top_m)
        chosen = {s for month_set in top.values() for s in month_set}
        metas = {s: m for s, m in metas.items() if s in chosen}
    frames: list[pl.DataFrame] = []
    funding: list[pl.DataFrame] = []
    for symbol in sorted(metas):
        frame, fund = store.read(metas[symbol])
        if top is not None:
            in_top = [m for m, chosen_set in top.items() if symbol in chosen_set]
            frame = frame.with_columns((pl.col("member") & _month("decision_ns").is_in(in_top)).alias("member"))
        frames.append(frame)
        funding.append(fund)
    if not frames:
        raise FileNotFoundError(f"no shard with member rows for the window {window} (missing: {missing})")
    files = sorted({f for m in metas.values() for f in m.files})
    evaluate_from = _month_start_ns(months[1]) if top_m is not None and len(months) > 1 else None
    panel = Panel(
        pl.concat(frames, how="diagonal_relaxed").sort(["decision_ns", "symbol"]),
        pl.concat(funding, how="diagonal_relaxed"),
        files,
        config,
        evaluate_from_ns=evaluate_from,
    )
    return UniversePanel(panel, built, reused, tuple(missing))
