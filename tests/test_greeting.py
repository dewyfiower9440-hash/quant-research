import pytest
from hypothesis import given
from hypothesis import strategies as st

from quant_research.domain.greeting import build_greeting
from quant_research.errors import ValidationError


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("dev", "Hello, dev!"),
        ("  padded  ", "Hello, padded!"),
        ("한글", "Hello, 한글!"),
    ],
)
def test_build_greeting_valid_name_returns_greeting(name: str, expected: str) -> None:
    assert build_greeting(name) == expected


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_build_greeting_blank_name_raises_validation_error(name: str) -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        build_greeting(name)


@given(st.text().filter(lambda s: s.strip() != ""))
def test_build_greeting_contains_stripped_name(name: str) -> None:
    result = build_greeting(name)
    assert result == f"Hello, {name.strip()}!"
