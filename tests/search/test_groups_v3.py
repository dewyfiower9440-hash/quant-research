"""v2.4 step 10 (ADR-055 decisions 1-2): the pre-registered round order of a v3 campaign (c2)."""

from __future__ import annotations

from quant_research.search.campaign import Cell, preregistered_groups, preregistered_groups_v3
from quant_research.strategy.space import INFEASIBLE_V3, MODEL_SPACES, REGIME_SPACES, SPACES_V3
from quant_research.strategy.templates import ALL_FAMILIES, MODEL_FAMILIES, REGIME_SUFFIX

FAMILIES = ("trend", "momentum", "funding_carry")


def keys(group: list[Cell]) -> set[str]:
    return {c.key for c in group}


def test_the_v3_rounds_run_the_old_grid_then_the_model_cells_then_the_wider_grid_then_the_gates() -> None:
    g = preregistered_groups_v3(FAMILIES)
    assert len(g) == 10
    # G1', G2': the grid c1 ran (4h / 1d, top 100), w2 then w1, without the cells that have no v3 space
    assert keys(g[0]) == {
        f"{f}@{tf}|top100|w2" for f in FAMILIES for tf in ("4h", "1d") if f"{f}@{tf}" not in INFEASIBLE_V3
    }
    assert "trend@1d|top100|w2" not in keys(g[0]) and "trend@4h|top100|w2" in keys(g[0])
    assert keys(g[1]) == {k.replace("|w2", "|w1") for k in keys(g[0])}
    # M0, M1-R on the same grid, both windows (the cross confirmation needs the pair)
    assert keys(g[2]) == {f"combo@{tf}|top100|{w}" for tf in ("4h", "1d") for w in ("w2", "w1")}
    assert keys(g[3]) == {f"ridge@{tf}|top100|{w}" for tf in ("4h", "1d") for w in ("w2", "w1")}
    # G3 (1h) and G4 (top 30), the model cells included
    assert all(c.timeframe == "1h" and c.top_m == 100 for c in g[4]) and "combo@1h|top100|w2" in keys(g[4])
    assert all(c.top_m == 30 for c in g[5]) and "ridge@4h|top30|w1" in keys(g[5])
    # R: the regime-gated families through the same four grid groups, last
    for group in g[6:]:
        assert all(c.family.endswith(REGIME_SUFFIX) for c in group)
    assert keys(g[6]) == {
        f"{f}{REGIME_SUFFIX}@{tf}|top100|w2"
        for f in FAMILIES
        for tf in ("4h", "1d")
        if f"{f}@{tf}" not in INFEASIBLE_V3
    }
    assert not any(f in c.family for group in g[6:] for c in group for f in MODEL_FAMILIES)  # no gated model cells


def test_every_v3_round_cell_has_a_registered_space_and_none_repeats() -> None:
    groups = preregistered_groups_v3(ALL_FAMILIES)
    seen: set[str] = set()
    for group in groups:
        for cell in group:
            assert cell.key not in seen, cell.key
            seen.add(cell.key)
            name = f"{cell.space}/v3"
            assert name in SPACES_V3 or name in MODEL_SPACES or name in REGIME_SPACES, name
    assert len(seen) == sum(len(g) for g in groups)


def test_the_v2_groups_are_unchanged() -> None:
    g = preregistered_groups(FAMILIES)
    assert len(g) == 4 and "trend@1d|top100|w2" in keys(g[0])  # c1's grid, infeasible cells included
