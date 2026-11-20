"""Paper market feed from the collected 1 s top-of-book samples (Phase 6 step 16; v2.2 A1 memory bound).

The T0 collector stores `binance.um.book_ticker_1s` (ADR-003); the paper engine builds 1-minute mid-price
bars from it (open/high/low/close of the mid, volume = sample count, quote_volume = displayed notional) and
assembles the same point-in-time panel the backtest uses, with `available_ns` = the sample's `received_ns`
(no modelled lag: these rows were observed live). Fills are simulated against the raw quotes that arrived
after the order (`quotes_after`). Only compacted lake files are read: raw segments are hourly and become
compactable one minute after their hour ends (compaction runs every 10 min), so the lake lags real time
by roughly 61-71 minutes. The paper tick therefore waits for `coverage_ns()` instead of the wall clock.

Memory (analysis A-1): a 45-day window of 1 s samples is tens of millions of rows. `LakeFeed` never
concatenates raw quotes over the decision window: it reduces each immutable lake file to partial minute bars
on its own (one file in memory at a time), caches the result per (file, size, symbols) for the life of the
process, and merges the partial bars. Raw quotes are read only for the short fill window of open orders."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import polars as pl

from quant_research.backtest.panel import MINUTE_NS, Panel, PanelConfig, assemble, decision_grid, settlements
from quant_research.capture.compress import hour_end_ns
from quant_research.data.lake import read_lake_file, read_lake_rows, segment_stem
from quant_research.paper.engine import Quote

BOOK_TICKER = "binance.um.book_ticker_1s"
HOUR_NS = 3_600 * 10**9
QUOTE_COLUMNS = ("symbol", "event_ns", "received_ns", "bid_price", "bid_qty", "ask_price", "ask_qty")
_QUOTE_SCHEMA = {
    "symbol": pl.String,
    "event_ns": pl.Int64,
    "received_ns": pl.Int64,
    "bid_price": pl.Float64,
    "bid_qty": pl.Float64,
    "ask_price": pl.Float64,
    "ask_qty": pl.Float64,
}
_BAR_SCHEMA = {
    "symbol": pl.String,
    "event_ns": pl.Int64,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "quote_volume": pl.Float64,
    "available_ns": pl.Int64,
}


@dataclass(frozen=True, slots=True)
class FeedWindow:
    first_ns: int
    last_ns: int


def _day(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, UTC).strftime("%Y-%m-%d")


def lake_files(data_root: Path, dataset_id: str, window: FeedWindow) -> list[Path]:
    """Lake files whose day folder may overlap the window (the folder name is the UTC day)."""
    lo, hi = _day(window.first_ns), _day(window.last_ns)
    out: list[Path] = []
    for day_dir in sorted((data_root / "lake" / dataset_id).glob("*")):
        if lo <= day_dir.name <= hi:
            out.extend(sorted(day_dir.glob("*.parquet")))
    return out


def load_quotes(data_root: Path, symbols: list[str], window: FeedWindow) -> pl.DataFrame:
    """Raw quotes of a (short) window, e.g. the fill window of one open order. Not for the decision window: the
    window filter is pushed down to each file's scan, so only the matching rows are materialised."""
    predicate = pl.col("symbol").is_in(symbols) & pl.col("received_ns").is_between(window.first_ns, window.last_ns)
    parts = [read_lake_rows(f, QUOTE_COLUMNS, predicate) for f in lake_files(data_root, BOOK_TICKER, window)]
    parts = [p for p in parts if p.height]
    if not parts:
        return pl.DataFrame(schema=_QUOTE_SCHEMA)
    return pl.concat(parts, how="diagonal_relaxed").sort(["symbol", "received_ns"])


def _partial_bars(quotes: pl.DataFrame) -> pl.DataFrame:
    """Minute bars of the mid plus the first receipt of each bar, so bars of adjacent files can be merged."""
    q = quotes.with_columns(
        ((pl.col("bid_price") + pl.col("ask_price")) / 2).alias("mid"),
        (pl.col("received_ns") // MINUTE_NS * MINUTE_NS).alias("event_ns"),
        ((pl.col("bid_price") * pl.col("bid_qty") + pl.col("ask_price") * pl.col("ask_qty")) / 2).alias("notional"),
    ).sort(["symbol", "received_ns"])
    return q.group_by("symbol", "event_ns", maintain_order=True).agg(
        pl.col("mid").first().alias("open"),
        pl.col("mid").max().alias("high"),
        pl.col("mid").min().alias("low"),
        pl.col("mid").last().alias("close"),
        pl.len().cast(pl.Float64).alias("volume"),
        pl.col("notional").sum().alias("quote_volume"),
        pl.col("received_ns").max().alias("available_ns"),
        pl.col("received_ns").min().alias("first_ns"),
    )


def merge_partial_bars(parts: list[pl.DataFrame]) -> pl.DataFrame:
    """Combine partial bars of the same minute from several files (open = earliest, close = latest)."""
    parts = [p for p in parts if p.height]
    if not parts:
        return pl.DataFrame(schema=_BAR_SCHEMA)
    frame = pl.concat(parts, how="vertical_relaxed").sort(["symbol", "event_ns", "first_ns"])
    return (
        frame.group_by("symbol", "event_ns", maintain_order=True)
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").sort_by("available_ns").last(),
            pl.col("volume").sum(),
            pl.col("quote_volume").sum(),
            pl.col("available_ns").max(),
        )
        .sort(["symbol", "event_ns"])
    )


def mid_minute_bars(quotes: pl.DataFrame) -> pl.DataFrame:
    """1m bars of the mid price per symbol, in the shape `assemble` expects (available_ns = last receipt)."""
    if quotes.height == 0:
        return pl.DataFrame(schema=_BAR_SCHEMA)
    return merge_partial_bars([_partial_bars(quotes)])


def panel_from_bars(
    bars: pl.DataFrame, symbols: list[str], config: PanelConfig, *, first_decision_ns: int, last_decision_ns: int
) -> Panel:
    decisions = decision_grid(first_decision_ns, last_decision_ns, config)
    frame = assemble(decisions, symbols, bars, config)
    return Panel(frame, settlements(None, None), [("paper-feed", bars.height)], config)


def panel_from_quotes(
    quotes: pl.DataFrame, symbols: list[str], config: PanelConfig, *, first_decision_ns: int, last_decision_ns: int
) -> Panel:
    return panel_from_bars(
        mid_minute_bars(quotes),
        symbols,
        config,
        first_decision_ns=first_decision_ns,
        last_decision_ns=last_decision_ns,
    )


def quotes_after(quotes: pl.DataFrame, symbol: str, since_ns: int, *, until_ns: int | None = None) -> list[Quote]:
    f = quotes.filter((pl.col("symbol") == symbol) & (pl.col("received_ns") >= since_ns))
    if until_ns is not None:
        f = f.filter(pl.col("received_ns") <= until_ns)
    return [
        Quote(symbol, int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        for r in f.select("received_ns", "bid_price", "bid_qty", "ask_price", "ask_qty").sort("received_ns").iter_rows()
    ]


def latest_mid(quotes: pl.DataFrame, symbol: str, at_ns: int) -> float | None:
    f = quotes.filter((pl.col("symbol") == symbol) & (pl.col("received_ns") <= at_ns)).sort("received_ns")
    if f.height == 0:
        return None
    row = f.tail(1).row(0, named=True)
    return (float(row["bid_price"]) + float(row["ask_price"])) / 2


def bar_mid(bars: pl.DataFrame, symbol: str, at_ns: int) -> float | None:
    """The last mid known at `at_ns` from minute bars (close of the latest bar available by then)."""
    f = bars.filter((pl.col("symbol") == symbol) & (pl.col("available_ns") <= at_ns)).sort("available_ns")
    if f.height == 0:
        return None
    return float(f.tail(1).row(0, named=True)["close"])


class MarketFeed(Protocol):
    def bars(self, symbols: list[str], window: FeedWindow) -> pl.DataFrame: ...

    def quotes(self, symbols: list[str], window: FeedWindow) -> pl.DataFrame: ...

    def covers(self, window: FeedWindow) -> bool:
        """True when every quote of the window is readable (nothing of it is still waiting for compaction)."""
        ...


@dataclass
class LakeFeed:
    """Bars over a long window with one lake file in memory at a time; cached per file for the process."""

    data_root: Path
    _cache: dict[tuple[str, int, tuple[str, ...]], pl.DataFrame] = field(default_factory=lambda: {})
    files_read: int = 0
    max_file_rows: int = 0
    _coverage: tuple[tuple[str, int], int | None] | None = None

    def _file_bars(self, path: Path, symbols: tuple[str, ...]) -> pl.DataFrame:
        key = (str(path), path.stat().st_size, symbols)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        raw = read_lake_file(path, QUOTE_COLUMNS)
        self.files_read += 1
        self.max_file_rows = max(self.max_file_rows, raw.height)
        part = raw.filter(pl.col("symbol").is_in(list(symbols)))
        bars = _partial_bars(part) if part.height else pl.DataFrame(schema={**_BAR_SCHEMA, "first_ns": pl.Int64})
        self._cache[key] = bars
        return bars

    def bars(self, symbols: list[str], window: FeedWindow) -> pl.DataFrame:
        """Minute bars that started in the window and were complete by its end (`available_ns <= last_ns`: no
        bar is visible before its last quote arrived). Partial bars are cut per file and cached, so a window
        that starts mid-minute may drop or shorten that first minute (the paper decision grid starts later)."""
        files = lake_files(self.data_root, BOOK_TICKER, window)
        syms = tuple(sorted(symbols))
        keep = {(str(f), f.stat().st_size, syms) for f in files}
        for key in [k for k in self._cache if k not in keep]:  # files that left the window, other symbol sets
            del self._cache[key]
        parts = [
            b.filter(pl.col("first_ns").is_between(window.first_ns, window.last_ns))
            for b in (self._file_bars(f, syms) for f in files)
        ]
        merged = merge_partial_bars(parts)
        return merged.filter(pl.col("available_ns") <= window.last_ns)

    def quotes(self, symbols: list[str], window: FeedWindow) -> pl.DataFrame:
        return load_quotes(self.data_root, symbols, window)

    def covers(self, window: FeedWindow) -> bool:
        """The lake reaches the window's end AND every raw segment whose hour overlaps the window has its lake
        file (same stem). The newest file alone is not enough: compaction skips a failed segment and goes on,
        so a later hour can be in the lake while an earlier one is not (review 3)."""
        top = self.coverage_ns()
        if top is None or top < window.last_ns:
            return False
        lake = {f.stem for f in lake_files(self.data_root, BOOK_TICKER, window)} | self._compacted_stems()
        lo, hi = _day(window.first_ns - HOUR_NS), _day(window.last_ns)
        for day in sorted((self.data_root / "raw" / BOOK_TICKER).glob("*")):
            if not (lo <= day.name <= hi):
                continue
            for seg in day.glob("*.jsonl*"):
                if seg.name.endswith(".tmp"):
                    continue
                end = hour_end_ns(seg.name.removesuffix(".gz"))
                if end is None or end <= window.first_ns or end - HOUR_NS > window.last_ns:
                    continue
                if segment_stem(seg) not in lake:
                    return False
        return True

    def _compacted_stems(self) -> set[str]:
        """Stems the compactor recorded (read-only catalog). A segment with 0 rows (e.g. the header-only segment
        of a collector restart) is recorded WITHOUT a lake file, so the lake folder alone cannot prove it done."""
        path = self.data_root / "meta" / "catalog.db"
        if not path.exists():
            return set()
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute("select stem from compacted_segment where dataset_id = ?", (BOOK_TICKER,)).fetchall()
        finally:
            conn.close()
        return {str(r[0]) for r in rows}

    def coverage_ns(self) -> int | None:
        """The latest `received_ns` in the newest compacted lake file (one small read, cached per file)."""
        days = sorted(d for d in (self.data_root / "lake" / BOOK_TICKER).glob("*") if d.is_dir())
        for day in reversed(days):
            files = sorted(day.glob("*.parquet"))
            if not files:
                continue
            newest = max(files, key=lambda f: f.name)
            key = (str(newest), newest.stat().st_size)
            if self._coverage is None or self._coverage[0] != key:
                top = read_lake_file(newest, ("received_ns",))["received_ns"].max()
                self._coverage = (key, int(top) if isinstance(top, int) else None)
            return self._coverage[1]
        return None
