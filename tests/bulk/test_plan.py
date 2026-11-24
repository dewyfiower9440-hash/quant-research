"""v2.5 step 2 (N02): the extras download reaches new months quickly and in the order the cells wait for them.

Measured before: every resume asked the catalog once per (symbol, month) with a full scan of `bulk_file` (0.26 s
each at 220,000 rows) and walked the symbols alphabetically, so a 105-minute tick spent itself on months it
already had; new months per tick fell from 806 to 77 and symbols after 'O' were never reached."""

from pathlib import Path

from quant_research.bulk.plan import (
    RESEARCH_WINDOWS,
    Checked,
    coverage,
    order_symbols,
    slowing,
    unavailable_months,
)
from quant_research.catalog.store import Catalog

W2, W1 = RESEARCH_WINDOWS
WANTED = ["2020-09", "2020-10", "2026-10"]


def test_the_research_windows_are_the_sealed_epoch_two_ones() -> None:
    assert (W2, W1) == (("2024-12", "2026-07"), ("2020-01", "2023-01"))


def test_symbols_are_ordered_by_what_the_open_window_needs_then_the_old_one_then_name() -> None:
    wants = {
        "AAA": {"2021-01", "2021-02", "2021-03"},  # three months of w1 only
        "BBB": {"2025-01"},  # one month of w2
        "CCC": {"2025-01", "2025-02", "2022-12"},  # two of w2, one of w1
        "DDD": {"2025-01", "2025-02"},  # two of w2
        "EEE": {"2023-06"},  # the sealed holdout: neither window
        "ABC": {"2023-07"},
    }
    assert order_symbols(wants) == ["CCC", "DDD", "BBB", "AAA", "ABC", "EEE"]


def test_window_bounds_are_half_open() -> None:
    wants = {"A": {"2024-11", "2026-07"}, "B": {"2024-12"}, "C": {"2026-06"}, "D": {"2023-01"}, "E": {"2022-12"}}
    assert order_symbols(wants) == ["B", "C", "E", "A", "D"]


def test_coverage_counts_wanted_pairs_of_a_window_without_the_unavailable_ones() -> None:
    wants = {"A": {"2024-12", "2025-01", "2025-02"}, "B": {"2025-01", "2021-05"}}
    have = {("A", "2024-12"), ("B", "2025-01"), ("B", "2021-05"), ("Z", "2025-01")}
    assert coverage(wants, have, W2) == (4, 2)
    assert coverage(wants, have, W1) == (1, 1)
    assert coverage(wants, have, W2, unavailable={("A", "2025-02")}) == (3, 2)
    assert coverage({}, set(), W2) == (0, 0)


def test_unavailable_months_are_the_wanted_ones_the_source_does_not_list() -> None:
    assert unavailable_months({"2020-09", "2021-12", "2022-01"}, listed={"2021-12", "2022-01", "2026-12"}) == [
        "2020-09"
    ]
    assert unavailable_months({"2021-12"}, listed={"2021-12"}) == []


def test_a_symbol_checked_complete_is_skipped_for_a_week_and_then_looked_at_again(tmp_path: Path) -> None:
    day = 86_400 * 10**9
    now = {"ns": 1_000 * day}
    path = tmp_path / "run" / "checked.json"
    checked = Checked.load(path, now_ns=lambda: now["ns"])
    assert checked.fresh("metrics", "BTCUSDT", WANTED) is False
    checked.mark("metrics", "BTCUSDT", wanted=WANTED, unavailable=["2020-09"])
    checked.save()
    again = Checked.load(path, now_ns=lambda: now["ns"])
    assert again.fresh("metrics", "BTCUSDT", WANTED) is True
    assert again.fresh("metrics", "ETHUSDT", WANTED) is False and again.fresh("fundingRate", "BTCUSDT", WANTED) is False
    assert again.unavailable("metrics") == {("BTCUSDT", "2020-09")}
    now["ns"] += 7 * day
    assert (
        again.fresh("metrics", "BTCUSDT", WANTED) is False
    )  # republished files of recent months are found within a week


def test_a_month_that_appears_later_is_no_longer_unavailable(tmp_path: Path) -> None:
    checked = Checked.load(tmp_path / "checked.json", now_ns=lambda: 5)
    checked.mark("metrics", "BTCUSDT", wanted=WANTED, unavailable=["2020-09", "2020-10"])
    checked.mark("metrics", "BTCUSDT", wanted=WANTED, unavailable=["2020-10"])
    assert checked.unavailable("metrics") == {("BTCUSDT", "2020-10")}


def test_a_checked_symbol_is_looked_at_again_as_soon_as_more_is_wanted_of_it(tmp_path: Path) -> None:
    """Review: the mark did not say what had been wanted. On the first of a month every active symbol wants one
    more month; a symbol marked in the days before was skipped for a week, the run ended 0 and the tier was done
    with that month missing. The same for a new universe file or a different --since."""
    path = tmp_path / "checked.json"
    checked = Checked.load(path, now_ns=lambda: 5)
    checked.mark("metrics", "BTCUSDT", wanted=["2026-09", "2026-10"], unavailable=[])
    checked.save()
    again = Checked.load(path, now_ns=lambda: 6)
    assert again.fresh("metrics", "BTCUSDT", ["2026-09", "2026-10"]) is True
    assert again.fresh("metrics", "BTCUSDT", ["2026-10"]) is True  # a narrower request (--since) was covered
    assert again.fresh("metrics", "BTCUSDT", ["2026-09", "2026-10", "2026-11"]) is False  # the month turned
    assert again.fresh("metrics", "BTCUSDT", ["2025-01"]) is False  # another universe file


def test_a_mark_without_its_wanted_months_is_never_fresh(tmp_path: Path) -> None:
    path = tmp_path / "checked.json"
    path.write_text('{"metrics": {"BTCUSDT": {"checked_ns": 5, "unavailable": []}}}', encoding="utf-8")
    assert Checked.load(path, now_ns=lambda: 6).fresh("metrics", "BTCUSDT", ["2026-10"]) is False


def test_an_unreadable_checked_file_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "checked.json"
    path.write_text("{", encoding="utf-8")
    assert Checked.load(path, now_ns=lambda: 5).fresh("metrics", "BTCUSDT", WANTED) is False
    path.write_text("[]", encoding="utf-8")
    assert Checked.load(path, now_ns=lambda: 5).unavailable("metrics") == set()


def test_slowing_is_two_ticks_in_a_row_under_half_the_median_of_the_three_before() -> None:
    assert slowing([806, 720, 610, 77, 89]) is True  # the measured stall: median 720, both under 360
    assert slowing([806, 720, 610, 390, 89]) is False  # only the last one
    assert slowing([806, 720, 610, 360, 359]) is False  # 360 is the boundary itself
    assert slowing([720, 610, 77, 89]) is False  # too short a history to say
    assert slowing([0, 0, 0, 0, 0]) is False  # nothing left to download is not a stall
    assert slowing([10, 806, 720, 610, 77, 89]) is True  # only the last five count


def test_the_catalog_finds_a_symbol_month_through_an_index(tmp_path: Path) -> None:
    catalog = Catalog.open(tmp_path / "catalog.db")
    try:
        plan = catalog.conn.execute(
            "explain query plan select url, last_modified from bulk_file "
            "where dataset_id = ? and symbol = ? and month = ?",
            ("d", "s", "m"),
        ).fetchall()
        assert "bulk_file_symbol_month" in str(plan) and "SCAN" not in str(plan)
    finally:
        catalog.close()


def test_the_index_migration_leaves_the_existing_rows_as_they_were(tmp_path: Path) -> None:
    path = tmp_path / "catalog.db"
    catalog = Catalog.open(path)
    rows = [(f"u{i}", "lm", "d", f"S{i % 3}", "2025-01", "sha", 1, 2, "p", 3) for i in range(9)]
    catalog.conn.executemany("insert into bulk_file values (?,?,?,?,?,?,?,?,?,?)", rows)
    # back to the schema before the index: the next open has the migration to apply
    catalog.conn.execute("drop index bulk_file_symbol_month")
    catalog.conn.execute("pragma user_version = 9")
    catalog.conn.commit()
    catalog.close()
    again = Catalog.open(path)
    try:
        assert again.schema_version() >= 10
        assert again.conn.execute("select * from bulk_file order by url").fetchall() == sorted(rows)
        names = [r[0] for r in again.conn.execute("select name from sqlite_master where type = 'index'")]
        assert "bulk_file_symbol_month" in names
    finally:
        again.close()
