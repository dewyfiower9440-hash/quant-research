"""Resource gate of the bulk download: every limit stops the run, boundaries are exact."""

import pytest

from quant_research.bulk.guard import GB, MB, Limits, Sample, combine, resource_gate

L = Limits()
OK = Sample(free_disk=300 * GB, bulk_bytes=10 * GB, available_ram=6 * GB, process_mem=300 * MB)


def test_gate_allows_when_all_resources_are_within_limits() -> None:
    assert resource_gate(OK, L) == (True, "ok")


@pytest.mark.parametrize(
    ("sample", "needle"),
    [
        (Sample(50 * GB - 1, 10 * GB, 6 * GB, 300 * MB), "free disk"),
        (Sample(300 * GB, 200 * GB, 6 * GB, 300 * MB), "cap"),
        (Sample(300 * GB, 10 * GB, 1536 * MB - 1, 300 * MB), "available RAM"),
        (Sample(300 * GB, 10 * GB, 6 * GB, 1024 * MB + 1), "process memory"),
    ],
)
def test_gate_stops_on_each_exceeded_limit(sample: Sample, needle: str) -> None:
    allowed, why = resource_gate(sample, L)
    assert not allowed and needle in why


def test_gate_boundaries_are_inclusive_for_allowed_values() -> None:
    edge = Sample(50 * GB, 200 * GB - 1, 1536 * MB, 1024 * MB)
    allowed, why = resource_gate(edge, L)
    assert allowed and why.startswith("WARN")  # above 80 % of the cap: allowed with a warning


def test_combine_returns_the_first_blocking_reason_or_all_reasons() -> None:
    assert combine((True, "a"), (False, "hot"), (False, "full")) == (False, "hot")
    assert combine((True, "a"), (True, "b")) == (True, "a; b")
