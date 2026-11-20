"""Phase 6 step 16: mid-price bars and panels from 1 s quotes, quote streams after an order, and one paper
tick with an in-memory ledger: orders are planned, risk-checked, submitted idempotently and settled."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from quant_research.backtest.panel import PanelConfig
from quant_research.config import Settings
from quant_research.data.lake import write_frame
from quant_research.paper.feed import (
    BOOK_TICKER,
    FeedWindow,
    LakeFeed,
    bar_mid,
    latest_mid,
    mid_minute_bars,
    panel_from_quotes,
    quotes_after,
)
from quant_research.paper.ledger import OrderIntent, PaperLedger
from quant_research.paper.run import (
    FILL_PAD_NS,
    PaperConfig,
    Strategy,
    account_exposure,
    daily_closes,
    health_due,
    load_policy,
    paper_health,
    paper_verifier,
    tick,
)
from quant_research.risk.limits import Limits
from quant_research.risk.policy import FakeVerifier, Policy, PolicyError, PolicyStore, policy_bytes
from quant_research.strategy.spec import StrategySpec
from quant_research.strategy.templates import templates

S = 10**9
M = 60 * S
H = 60 * M
T0 = 1_767_225_600 * S
SYMBOLS = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
NOW = T0 + 99 * H + 30 * M


def _quotes(symbols: list[str], hours: int, seed: int = 1) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    for k, s in enumerate(symbols):
        mid = 100.0 * (k + 1)
        for i in range(hours * 3600 // 10):  # a quote every 10 s
            mid *= float(np.exp(0.0002 * rng.standard_normal()))
            t = T0 + i * 10 * S
            rows.append(
                {
                    "symbol": s,
                    "event_ns": t - 50_000_000,
                    "received_ns": t,
                    "bid_price": mid * 0.9999,
                    "bid_qty": 5.0,
                    "ask_price": mid * 1.0001,
                    "ask_qty": 5.0,
                }
            )
    return pl.DataFrame(rows)


def test_minute_bars_and_panel_from_quotes() -> None:
    q = _quotes(["AAAUSDT", "BBBUSDT"], hours=3)
    bars = mid_minute_bars(q)
    assert bars.height == 2 * 180 and set(bars.columns) >= {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "available_ns",
    }
    assert (bars["volume"] == 6.0).all() and (bars["high"] >= bars["low"]).all()
    assert (bars["available_ns"] >= bars["event_ns"]).all()  # available after the minute started
    cfg = PanelConfig(H, decision_delay_ns=0, latency_ns=0)
    panel = panel_from_quotes(q, ["AAAUSDT", "BBBUSDT"], cfg, first_decision_ns=T0 + H, last_decision_ns=T0 + 2 * H)
    assert panel.frame["decision_ns"].n_unique() == 2 and set(panel.frame["symbol"].unique()) == {"AAAUSDT", "BBBUSDT"}
    assert panel.frame["close"].null_count() == 0
    stream = quotes_after(q, "AAAUSDT", T0 + H, until_ns=T0 + H + 30 * S)
    assert len(stream) == 4 and all(s.symbol == "AAAUSDT" and s.at_ns >= T0 + H for s in stream)
    assert latest_mid(q, "BBBUSDT", T0 + 5 * S) is not None and latest_mid(q, "ZZZ", T0) is None
    assert mid_minute_bars(q.head(0)).height == 0


def _paper_setup(
    tmp_path: Path,
) -> tuple[Settings, PaperLedger, PolicyStore, Strategy, PaperConfig, pl.DataFrame, dict[str, int]]:
    """A strategy that DOES trade on the fixture (mean reversion, equal sizing; 100 h of quotes so the 48 h
    z-score is warm inside the 3-day decision window): the tests below assert on real orders."""
    q = _quotes(SYMBOLS, hours=100, seed=3)
    clock = {"t": NOW}  # the ledger's clock = the tick's now (submission times are on the same clock)
    ledger = PaperLedger.open(tmp_path / "ledger.db", now_ns=lambda: clock["t"])
    verifier = FakeVerifier(b"k")
    policy = PolicyStore(verifier)
    limits = Limits(50_000.0, 20_000.0, 3.0, 5_000.0, 5_000.0, 5_000.0, 100, 20_000.0)
    msg = policy_bytes(Policy(1, limits, 0, T0 + 10**6 * S))
    policy.apply(msg, verifier.sign(msg), now_ns=T0)
    body = templates()["mean_reversion"].model_dump(mode="json")
    body["universe"] = {"selector": "pit_top_liquidity", "n": 2, "rebalance": "1d"}
    body["sizing"] = {**body["sizing"], "method": "equal"}
    strat = Strategy("h-mr", StrategySpec.model_validate(body), "Paper")
    cfg = PaperConfig("paper-test", capital=10_000.0, lookback_days=4, latency_ns=M, max_wait_ns=5 * M)
    return Settings(data_root=tmp_path), ledger, policy, strat, cfg, q, clock


def test_one_paper_tick_plans_risk_checks_submits_and_settles(tmp_path: Path) -> None:
    settings, ledger, policy, strat, cfg, q, clock = _paper_setup(tmp_path)
    feed = _FrameFeed(q, visible_until=NOW)  # a lake cannot hold quotes after the tick's own time
    first = tick(settings, cfg, ledger, policy, now_ns=NOW, symbols=SYMBOLS, strategies=[strat], feed=feed)
    assert first["quotes"] == 0 and int(str(first["bars"])) > 0  # no open orders yet: bars only
    assert first.get("policy") is None and "reconcile" not in first
    assert int(str(first["submitted"])) >= 1  # the precondition of everything below
    open_before = ledger.open_orders()
    assert open_before and all(o.state == "PENDING_SUBMIT" for o in open_before)
    again = tick(settings, cfg, ledger, policy, now_ns=NOW, symbols=SYMBOLS, strategies=[strat], feed=feed)
    assert again["submitted"] == 0  # same decision -> same client ids -> idempotent
    assert again["refused"] == 0  # the open order is not planned again (review: phantom refusals)
    assert again["settled"] == 0  # submitted at NOW: no quote after its arrival exists yet (no fill from the past)
    clock["t"] = NOW + 20 * M
    feed.visible_until = NOW + 20 * M
    later = tick(settings, cfg, ledger, policy, now_ns=NOW + 20 * M, symbols=SYMBOLS, strategies=[strat], feed=feed)
    # the quotes after arrival exist in the feed: every order settled (filled, or canceled after max_wait)
    assert int(str(later["settled"])) >= 1 and ledger.open_orders() == []  # every order settled
    assert int(str(later["quotes"])) > 0 and "slippage_ref_missing" not in later
    assert ledger.verify_chain() == (True, "ok") and ledger.reconcile() == []
    empty = PolicyStore(FakeVerifier(b"k"))
    no_policy = tick(settings, cfg, ledger, empty, now_ns=NOW, symbols=SYMBOLS, strategies=[strat], feed=feed)
    assert "no policy" in str(no_policy["policy"]) and no_policy["submitted"] == 0
    ledger.close()


class _FrameFeed:
    """In-memory feed over one quote frame (the lake feed is tested separately below). `visible_until` models
    the lake's lag (hourly segments are compacted about an hour late): nothing after it can be read."""

    def __init__(self, quotes: pl.DataFrame, visible_until: int | None = None) -> None:
        self.q = quotes
        self.visible_until = visible_until
        self.quote_windows: list[FeedWindow] = []

    def _window(self, symbols: list[str], window: FeedWindow) -> pl.DataFrame:
        last = window.last_ns if self.visible_until is None else min(window.last_ns, self.visible_until)
        return self.q.filter(pl.col("symbol").is_in(symbols) & pl.col("received_ns").is_between(window.first_ns, last))

    def covers(self, window: FeedWindow) -> bool:
        top = self.q["received_ns"].max()
        if not isinstance(top, int):
            return False
        return (top if self.visible_until is None else min(top, self.visible_until)) >= window.last_ns

    def bars(self, symbols: list[str], window: FeedWindow) -> pl.DataFrame:
        return mid_minute_bars(self._window(symbols, window))

    def quotes(self, symbols: list[str], window: FeedWindow) -> pl.DataFrame:
        self.quote_windows.append(window)
        return self._window(symbols, window)


def _write_hourly_files(root: Path, q: pl.DataFrame) -> list[Path]:
    """The collector's layout: one compacted file per hour under the UTC day folder."""
    out: list[Path] = []
    hours = q.with_columns((pl.col("received_ns") // H).alias("h")).partition_by("h", as_dict=True)
    for (h,), part in sorted(hours.items(), key=lambda kv: int(str(kv[0][0]))):
        day = "2026-01-01" if int(str(h)) * H < T0 + 24 * H else "2026-01-02"
        target = root / "lake" / BOOK_TICKER / day / f"{BOOK_TICKER}_{h}.parquet"
        write_frame(part.drop("h"), target)
        out.append(target)
    return out


def test_lake_feed_bars_match_one_pass_bars_and_read_one_file_at_a_time(tmp_path: Path) -> None:
    q = _quotes(["AAAUSDT", "BBBUSDT"], hours=5, seed=7)
    files = _write_hourly_files(tmp_path, q)
    feed = LakeFeed(tmp_path)
    window = FeedWindow(T0, T0 + 5 * H)
    bars = feed.bars(["AAAUSDT", "BBBUSDT"], window)
    expected = mid_minute_bars(q)
    assert bars.height == expected.height == 2 * 300
    assert bars.equals(expected)  # every column
    assert feed.files_read == len(files) == 5
    assert feed.max_file_rows == 2 * 360  # never more than one hourly file (2 symbols x 360 quotes) in memory
    again = feed.bars(["AAAUSDT", "BBBUSDT"], window)
    assert feed.files_read == 5 and again.equals(bars)  # cached per file: no re-read
    assert bar_mid(bars, "AAAUSDT", T0 + 2 * H) is not None and bar_mid(bars, "ZZZ", T0 + 2 * H) is None


def test_lake_feed_merges_a_minute_split_across_two_files(tmp_path: Path) -> None:
    q = _quotes(["AAAUSDT"], hours=1, seed=2)
    early = q.filter(pl.col("received_ns") < T0 + 30 * S)  # quotes at 0, 10, 20 s of the first minute
    late = q.filter(pl.col("received_ns") >= T0 + 30 * S)
    # file names in the REVERSE of time order: the merge must order by receipt time, not by file name
    write_frame(early, tmp_path / "lake" / BOOK_TICKER / "2026-01-01" / "z-early.parquet")
    write_frame(late, tmp_path / "lake" / BOOK_TICKER / "2026-01-01" / "a-late.parquet")
    bars = LakeFeed(tmp_path).bars(["AAAUSDT"], FeedWindow(T0, T0 + H))
    assert bars.equals(mid_minute_bars(q))  # every column: open/high/low/close/volume/quote_volume/available_ns


def test_lake_feed_shows_only_bars_complete_by_the_end_of_the_window(tmp_path: Path) -> None:
    q = _quotes(["AAAUSDT"], hours=1, seed=5)
    write_frame(q, tmp_path / "lake" / BOOK_TICKER / "2026-01-01" / "one.parquet")
    bars = LakeFeed(tmp_path).bars(["AAAUSDT"], FeedWindow(T0, T0 + 10 * M + 15 * S))
    last = bars["available_ns"].to_list()[-1]
    assert bars.height == 10 and isinstance(last, int) and last <= T0 + 10 * M + 15 * S  # no look-ahead


def test_lake_feed_drops_files_that_left_the_window_and_reads_quotes_only_for_the_fill_window(tmp_path: Path) -> None:
    q = _quotes(["AAAUSDT"], hours=30, seed=4)
    _write_hourly_files(tmp_path, q)
    feed = LakeFeed(tmp_path)
    feed.bars(["AAAUSDT"], FeedWindow(T0, T0 + 30 * H))
    assert len(feed._cache) == 30  # pyright: ignore[reportPrivateUsage]
    feed.bars(["AAAUSDT"], FeedWindow(T0 + 25 * H, T0 + 30 * H))  # only the second day folder remains
    assert len(feed._cache) == 6  # pyright: ignore[reportPrivateUsage]
    fill = feed.quotes(["AAAUSDT"], FeedWindow(T0 + 26 * H, T0 + 26 * H + 5 * M))
    assert fill.height == 31 and fill["received_ns"].min() == T0 + 26 * H


def test_tick_reads_raw_quotes_only_in_each_open_orders_fill_window(tmp_path: Path) -> None:
    settings, ledger, policy, strat, cfg, q, clock = _paper_setup(tmp_path)
    feed = _FrameFeed(q, visible_until=NOW)
    tick(settings, cfg, ledger, policy, now_ns=NOW, symbols=SYMBOLS, strategies=[strat], feed=feed)
    assert feed.quote_windows == []  # no open orders yet: no raw quote read
    orders = ledger.open_orders()
    assert orders  # precondition: the strategy traded
    clock["t"] = NOW + 20 * M
    feed.visible_until = NOW + 20 * M
    tick(settings, cfg, ledger, policy, now_ns=NOW + 20 * M, symbols=SYMBOLS, strategies=[strat], feed=feed)
    assert len(feed.quote_windows) == len(orders)  # one short window per open order
    for w in feed.quote_windows:
        assert w.last_ns - w.first_ns == FILL_PAD_NS + cfg.max_wait_ns  # minutes, not the 4-day decision window
        assert w.first_ns == NOW + cfg.latency_ns - FILL_PAD_NS  # from the SUBMISSION time, not the decision hour
    ledger.close()


def test_the_paper_launcher_sets_the_polars_thread_limit_before_polars_is_imported() -> None:
    """Polars reads POLARS_MAX_THREADS at import: the launcher and the package init must not import anything
    that pulls Polars in before `main()` sets the limit (static check, no subprocess)."""
    import ast

    pkg = Path(__file__).resolve().parents[2] / "src" / "quant_research" / "paper"
    for name in ("__init__.py", "__main__.py"):
        tree = ast.parse((pkg / name).read_text(encoding="utf-8"))
        top = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
        modules = [m.module or "" for m in top if isinstance(m, ast.ImportFrom)] + [
            a.name for m in top if isinstance(m, ast.Import) for a in m.names
        ]
        assert set(modules) <= {"__future__", "os"}, (name, modules)


def _write_policy(root: Path, verifier: FakeVerifier, version: int, *, max_order: float = 5_000.0) -> None:
    folder = root / "paper"
    folder.mkdir(parents=True, exist_ok=True)
    limits = Limits(50_000.0, 20_000.0, 3.0, 5_000.0, 5_000.0, 5_000.0, 100, max_order)
    msg = policy_bytes(Policy(version, limits, 0, T0 + 10**6 * S))
    (folder / "policy.json").write_bytes(msg)
    (folder / "policy.sig").write_text(verifier.sign(msg).hex(), encoding="utf-8")


def test_reloading_the_same_policy_every_tick_is_a_no_op_and_a_new_version_replaces_it(tmp_path: Path) -> None:
    """Review finding 1: the loop re-applied the same policy file every tick and the second apply raised
    'rollback refused', so every tick after the first failed."""
    verifier = FakeVerifier(b"k")
    store = PolicyStore(verifier)
    _write_policy(tmp_path, verifier, 1)
    load_policy(tmp_path, store, now_ns=T0)
    load_policy(tmp_path, store, now_ns=T0 + M)  # the same file again: no error, same policy in force
    assert store.active(now_ns=T0 + M).version == 1 and store.history == [1]
    _write_policy(tmp_path, verifier, 2, max_order=1_000.0)
    load_policy(tmp_path, store, now_ns=T0 + 2 * M)
    assert store.active(now_ns=T0 + 2 * M).limits.max_order_notional == 1_000.0 and store.history == [1, 2]
    _write_policy(tmp_path, verifier, 1)  # an older version written back: still a refused rollback
    with pytest.raises(PolicyError, match="rollback refused"):
        load_policy(tmp_path, store, now_ns=T0 + 3 * M)


def test_without_a_policy_key_every_policy_is_refused(tmp_path: Path) -> None:
    """Review (low): a missing key became an EMPTY HMAC key, which anyone can sign with (fail-open)."""
    verifier = paper_verifier(tmp_path)
    store = PolicyStore(verifier)
    _write_policy(tmp_path, FakeVerifier(b""), 1)  # signed with the empty key
    with pytest.raises(PolicyError, match="signature does not verify"):
        load_policy(tmp_path, store, now_ns=T0)
    (tmp_path / "paper" / "policy.key").write_bytes(b"secret")
    _write_policy(tmp_path, FakeVerifier(b"secret"), 1)
    store2 = PolicyStore(paper_verifier(tmp_path))
    load_policy(tmp_path, store2, now_ns=T0)
    assert store2.active(now_ns=T0).version == 1


def test_account_exposure_counts_open_orders_unrealized_losses_and_the_order_rate(tmp_path: Path) -> None:
    clock = {"t": NOW}
    ledger = PaperLedger.open(tmp_path / "l.db", now_ns=lambda: clock["t"])
    ledger.submit(OrderIntent("f1", "run", "A", "binance", "AAAUSDT", "buy", 10.0, None, False, NOW))
    ledger.ack("f1", "v1")
    ledger.fill("f1", 10.0, 100.0, fee=1.0, liquidity="taker", at_ns=NOW)
    ledger.submit(OrderIntent("o1", "run", "A", "binance", "BBBUSDT", "sell", 5.0, None, False, NOW))  # still open
    e = account_exposure(ledger, {"AAAUSDT": 90.0, "BBBUSDT": 200.0}, capital=10_000.0, now_ns=NOW + M)
    assert e.per_symbol == {"AAAUSDT": 900.0, "BBBUSDT": -1_000.0} and e.gross_notional == 1_900.0
    assert e.daily_pnl == pytest.approx(-100.0 - 1.0) and e.total_pnl == pytest.approx(-101.0)  # 10 x (90-100) - fee
    assert e.orders_last_minute == 2 and e.equity == pytest.approx(10_000.0 - 101.0)
    ledger.close()


def test_daily_health_degrades_a_losing_paper_strategy(tmp_path: Path) -> None:
    from quant_research.validator.lifecycle import LifecycleLedger, Transition
    from quant_research.validator.store import ValidatorStore

    q = _quotes(["AAAUSDT"], hours=24 * 12, seed=8)
    bars = mid_minute_bars(q.filter(pl.col("received_ns") % (60 * S) == 0))  # one quote a minute is enough here
    closes = daily_closes(bars, before_ns=T0 + 12 * 24 * H)
    assert len(closes) == 12 and all("AAAUSDT" in c for c in closes.values())
    clock = {"t": T0}
    ledger = PaperLedger.open(tmp_path / "l.db", now_ns=lambda: clock["t"])
    for d in range(11):  # every day: buy at 5 % above the day's close, a steady loss the CUSUM must catch
        t = T0 + d * 24 * H + 12 * H
        clock["t"] = t
        cid = f"d{d}"
        ledger.submit(OrderIntent(cid, "run", "h", "binance", "AAAUSDT", "buy", 1.0, None, False, t))
        ledger.ack(cid, cid)
        ledger.fill(cid, 1.0, closes[T0 + d * 24 * H]["AAAUSDT"] * 1.05, fee=0.0, liquidity="taker", at_ns=t)
    store = ValidatorStore.open(tmp_path / "v.db")
    try:
        lc = LifecycleLedger(store.conn, now_ns=lambda: 1)
        lc.move(Transition("h", None, "Candidate", "s", "trial:1", "research"))
        lc.move(Transition("h", "Candidate", "Validated", "v", "verdict:1", "validator"))
        lc.move(Transition("h", "Validated", "Paper", "p", "paper:x", "paper"))
        spec = StrategySpec.model_validate(templates()["trend"].model_dump(mode="json"))
        cfg = PaperConfig("paper-test", capital=1_000.0)
        strat = Strategy("h", spec, "Paper", expected=(0.0005, 0.002))
        moved = paper_health(ledger, lc, bars, [strat], cfg, now_ns=T0 + 12 * 24 * H)
        assert moved == {"h": "Degraded"} and lc.state("h") == "Degraded"
    finally:
        store.close()
        ledger.close()


def test_an_order_waits_for_the_lake_to_cover_its_fill_window_instead_of_being_canceled(tmp_path: Path) -> None:
    """Review 2 finding 1: the lake lags about an hour (hourly segments, compacted after the hour); canceling on
    the wall clock canceled every paper order before its quotes were readable."""
    settings, ledger, policy, strat, cfg, q, clock = _paper_setup(tmp_path)
    tick(settings, cfg, ledger, policy, now_ns=NOW, symbols=SYMBOLS, strategies=[strat], feed=_FrameFeed(q, NOW))
    first_ids = {o.client_id for o in ledger.open_orders()}
    assert first_ids
    q_more = _quotes(SYMBOLS, hours=103, seed=3)  # the market continues; the lake shows it an hour late
    lagged = _FrameFeed(q_more, visible_until=NOW + 3 * M)  # fill window = NOW + 1 .. NOW + 6 min: not covered
    clock["t"] = NOW + 20 * M
    waiting = tick(settings, cfg, ledger, policy, now_ns=NOW + 20 * M, symbols=SYMBOLS, strategies=[strat], feed=lagged)
    assert int(str(waiting.get("waiting", 0))) >= 1 and waiting["canceled"] == 0 and ledger.open_orders()
    lagged.visible_until = NOW + 2 * H
    clock["t"] = NOW + 90 * M
    done = tick(settings, cfg, ledger, policy, now_ns=NOW + 90 * M, symbols=SYMBOLS, strategies=[strat], feed=lagged)
    assert int(str(done["settled"])) >= 1
    assert first_ids.isdisjoint(o.client_id for o in ledger.open_orders())  # settled (a new decision may trade)
    assert all(ledger.order(c).state == "FILLED" for c in first_ids)  # type: ignore[union-attr]
    ledger.close()


def test_no_decision_on_bars_older_than_the_decision_hour(tmp_path: Path) -> None:
    """Review 2 finding 5: an empty-panel guard could never fire (the panel is symbols x decisions); a decision on
    stale bars must be skipped instead."""
    settings, ledger, policy, strat, cfg, q, _clock = _paper_setup(tmp_path)
    stale = _FrameFeed(q, visible_until=NOW - 3 * H)  # the decision hour is NOW - 90 min; bars end 90 min earlier
    out = tick(settings, cfg, ledger, policy, now_ns=NOW, symbols=SYMBOLS, strategies=[strat], feed=stale)
    assert out["submitted"] == 0 and out.get("stale") == SYMBOLS and ledger.open_orders() == []
    ledger.close()


def test_lake_feed_coverage_is_the_latest_receipt_in_the_newest_compacted_file(tmp_path: Path) -> None:
    assert LakeFeed(tmp_path).coverage_ns() is None  # no lake yet
    q = _quotes(["AAAUSDT"], hours=30, seed=6)
    _write_hourly_files(tmp_path, q)
    feed = LakeFeed(tmp_path)
    assert feed.coverage_ns() == int(str(q["received_ns"].max()))
    assert feed.coverage_ns() == int(str(q["received_ns"].max()))  # cached per newest file


def test_a_window_is_covered_only_when_every_overlapping_segment_is_compacted(tmp_path: Path) -> None:
    """Review 3: the newest lake file does not prove the hours before it are in the lake (compaction skips a failed
    segment and goes on); an order settled against the missing hour would be canceled with zero quotes."""
    q = _quotes(["AAAUSDT"], hours=3, seed=6)
    lake = _write_hourly_files(tmp_path, q)
    raw = tmp_path / "raw" / BOOK_TICKER / "2026-01-01"
    raw.mkdir(parents=True)
    for f in lake:  # the raw segments of the same hours (the collector's names; compaction keeps the stem)
        hour = int(f.stem.rsplit("_", 1)[-1])
        name = datetime.fromtimestamp(hour * 3600, UTC).strftime("%Y%m%dT%H")
        (raw / f"{BOOK_TICKER}_{name}_{hour}.jsonl.gz").write_bytes(b"")
        f.rename(f.with_name(f"{BOOK_TICKER}_{name}_{hour}.parquet"))
    feed = LakeFeed(tmp_path)
    window = FeedWindow(T0 + H + 10 * M, T0 + H + 20 * M)
    assert feed.covers(window)
    missing = next((tmp_path / "lake" / BOOK_TICKER / "2026-01-01").glob("*T01_*.parquet"))
    missing.unlink()  # the second hour failed to compact; the third is in the lake
    feed2 = LakeFeed(tmp_path)
    assert not feed2.covers(window) and feed2.covers(FeedWindow(T0 + 2 * H + 10 * M, T0 + 2 * H + 20 * M))
    assert not feed2.covers(FeedWindow(T0 + 2 * H + 10 * M, T0 + 4 * H))  # beyond the newest receipt


def test_daily_health_waits_until_the_lake_covers_the_previous_day() -> None:
    day = 24 * H
    after_midnight = _quotes(["AAAUSDT"], hours=1).with_columns(pl.lit(T0 + 10 * day + M).alias("received_ns"))
    # the market data exists past midnight, but the lake shows only up to 23:30: the lag alone must defer health
    lagging = _FrameFeed(after_midnight, visible_until=T0 + 10 * day - 30 * M)
    assert not health_due(lagging, now_ns=T0 + 10 * day + 5 * M, last_day=None)  # 00:05: yesterday not complete
    lagging.visible_until = T0 + 10 * day + 5 * M
    assert health_due(lagging, now_ns=T0 + 10 * day + 70 * M, last_day=None)
    assert not health_due(lagging, now_ns=T0 + 10 * day + 70 * M, last_day=(T0 + 10 * day) // day)  # done today


def test_a_header_only_segment_compacted_to_zero_rows_does_not_block_coverage(tmp_path: Path) -> None:
    """Review 4: a collector restart leaves a header-only raw segment; compaction records it with 0 rows and NO lake
    file, so a lake-file check alone kept that hour uncovered forever (orders stuck ACKED, health never due)."""
    from quant_research.catalog.store import Catalog

    q = _quotes(["AAAUSDT"], hours=3, seed=6)
    lake = _write_hourly_files(tmp_path, q)
    raw = tmp_path / "raw" / BOOK_TICKER / "2026-01-01"
    raw.mkdir(parents=True)
    for f in lake:
        hour = int(f.stem.rsplit("_", 1)[-1])
        name = datetime.fromtimestamp(hour * 3600, UTC).strftime("%Y%m%dT%H")
        (raw / f"{BOOK_TICKER}_{name}_{hour}.jsonl.gz").write_bytes(b"")
        f.rename(f.with_name(f"{BOOK_TICKER}_{name}_{hour}.parquet"))
    t01 = datetime.fromtimestamp((T0 + H) // S, UTC).strftime("%Y%m%dT%H")
    restart = f"{BOOK_TICKER}_{t01}_1767229999000000000"  # the collector names segments <hour>_<open ns>
    (raw / f"{restart}.jsonl.gz").write_bytes(b"")  # the header-only segment of a restart in the same hour
    window = FeedWindow(T0 + H + 10 * M, T0 + H + 20 * M)
    assert not LakeFeed(tmp_path).covers(window)  # not compacted yet: waits
    catalog = Catalog.open(tmp_path / "meta" / "catalog.db")
    catalog.conn.execute(
        "insert into compacted_segment(stem, dataset_id, segment_sha256, records, rows_written, skipped_bytes, "
        "lake_path, compacted_ns) values (?, ?, 'x', 0, 0, 0, null, 1)",
        (restart, BOOK_TICKER),
    )
    catalog.close()
    assert LakeFeed(tmp_path).covers(window)  # compacted with 0 rows: nothing to wait for
