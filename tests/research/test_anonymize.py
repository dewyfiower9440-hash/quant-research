"""Phase 3 step 2: anonymization (aliases, leak detection, redaction)."""

from collections.abc import Generator
from pathlib import Path

import pytest

from quant_research.research.anonymize import Anonymizer, forbidden_terms, leaks, redact
from quant_research.research.features import evidence_ref
from quant_research.research.store import ResearchStore


@pytest.fixture
def store(tmp_path: Path) -> Generator[ResearchStore]:
    s = ResearchStore.open(tmp_path / "r.db")
    yield s
    s.close()


def test_aliases_are_stable_unique_and_reversible_only_locally(store: ResearchStore) -> None:
    a = Anonymizer(store)
    assert [a.alias("symbol", s) for s in ("BTCUSDT", "ETHUSDT", "BTCUSDT")] == ["S01", "S02", "S01"]
    assert a.real("symbol", "S02") == "ETHUSDT"
    with pytest.raises(KeyError, match="unknown symbol alias"):
        a.real("symbol", "S99")


def test_forbidden_terms_include_base_assets_and_contract_prefixes() -> None:
    assert forbidden_terms(["BTCUSDT", "1000SHIBUSDT"]) == {"BTCUSDT", "BTC", "1000SHIBUSDT", "SHIB"}


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ('{"note": "BTC broke out"}', "term:BTC"),
        ("btcusdt momentum", "term:BTCUSDT"),
        ("in 2025-03 the trend", "date:2025-03"),
        ("on 2024/11/05", "date:2024/11/05"),
        ("t=1767225600000", "epoch:1767225600000"),
        ("ns 1767225600000000000", "epoch:1767225600000000000"),
    ],
)
def test_leaks_find_symbols_dates_and_epochs(text: str, kind: str) -> None:
    assert kind in leaks(text, forbidden_terms(["BTCUSDT"]))


def test_clean_payloads_and_lookalike_words_do_not_trip_the_check() -> None:
    clean = '{"families": [{"family": "trend", "cycles": 3}], "universe": ["S01", "S02"], "sr": -0.12, "n": 4344}'
    assert leaks(clean, forbidden_terms(["BTCUSDT", "ETHUSDT"])) == []
    assert leaks("ethics and methods", forbidden_terms(["ETHUSDT"])) == []  # ETH inside a word is not ETH


def test_redact_removes_terms_dates_and_epochs_from_untrusted_text() -> None:
    text = "BTC rallied on 2025-03-02 (ts 1767225600000); btcusdt short failed"
    out = redact(text, forbidden_terms(["BTCUSDT"]))
    assert out == "[asset] rallied on [date] (ts [time]); [asset] short failed"
    assert leaks(out, forbidden_terms(["BTCUSDT"])) == []


def test_evidence_refs_are_typed_strings() -> None:
    assert evidence_ref("trial", 7) == "trial:7"
    with pytest.raises(ValueError, match="positive"):
        evidence_ref("feature_eval", 0)
