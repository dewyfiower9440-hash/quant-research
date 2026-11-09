"""Plan step 11 arithmetic: span union, intersection, coverage."""

import pytest

from quant_research.monitor.validate import covered_fraction, intersect, merge_spans


def test_merge_clips_to_the_window_joins_overlaps_and_drops_unknown_starts() -> None:
    spans = [(5, 15), (12, 20), (30, 40), (None, 50), (-10, 2), (95, 200)]
    assert merge_spans(spans, 0, 100) == [(0, 2), (5, 20), (30, 40), (95, 100)]


def test_touching_spans_are_joined() -> None:
    assert merge_spans([(0, 10), (10, 20)], 0, 100) == [(0, 20)]


def test_intersection_keeps_only_time_missing_on_both_sides() -> None:
    assert intersect([(0, 10), (20, 30)], [(5, 25)]) == [(5, 10), (20, 25)]
    assert intersect([(0, 10)], [(10, 20)]) == []


def test_coverage_is_one_minus_the_missing_share() -> None:
    assert covered_fraction(0, 100, [(0, 10), (50, 60)]) == pytest.approx(0.8)
    with pytest.raises(ValueError, match="empty validation window"):
        covered_fraction(5, 5, [])
