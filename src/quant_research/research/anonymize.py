"""Anonymization of everything the Director sends to an LLM (design v2.1 §9-6, §10: "LLM에는 익명화한 요약과
관련 레코드만").

- symbols become stable aliases (S01, S02, ...); the map lives in research.db (`alias`) and never leaves it;
- payloads carry no dates or epoch timestamps (relative indices only) and no price levels (returns, ratios
  and counts only), so the model cannot recognise a period or an asset from the numbers;
- `leaks()` is the last check before a request is sent: any real symbol, base asset, date or epoch-looking
  number in the serialized payload is a leak; `redact()` removes them from untrusted text (findings written
  from earlier LLM output) before it is shown again.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from quant_research.research.store import ResearchStore

Kind = Literal["symbol"]
_DATE = re.compile(r"\b(?:19|20)\d{2}[-/.](?:0[1-9]|1[0-2])(?:[-/.](?:0[1-9]|[12]\d|3[01]))?\b")
_EPOCH = re.compile(r"\b1[5-9]\d{8}(?:\d{3}){0,3}\b")  # epoch seconds / ms / us / ns from 2017 on


class Anonymizer:
    def __init__(self, store: ResearchStore) -> None:
        self.store = store

    def alias(self, kind: Kind, real: str) -> str:
        row = self.store.conn.execute("select alias from alias where kind = ? and real = ?", (kind, real)).fetchone()
        if row is not None:
            return str(row[0])
        n = int(self.store.conn.execute("select count(*) from alias where kind = ?", (kind,)).fetchone()[0])
        name = f"S{n + 1:02d}"
        self.store.conn.execute("insert into alias(kind, real, alias) values (?,?,?)", (kind, real, name))
        return name

    def real(self, kind: Kind, alias: str) -> str:
        row = self.store.conn.execute("select real from alias where kind = ? and alias = ?", (kind, alias)).fetchone()
        if row is None:
            raise KeyError(f"unknown {kind} alias {alias!r}")
        return str(row[0])


def forbidden_terms(symbols: Iterable[str]) -> set[str]:
    """Real symbols and their base assets (BTCUSDT -> BTC, 1000SHIBUSDT -> SHIB)."""
    out: set[str] = set()
    for s in symbols:
        out.add(s.upper())
        base = s.upper().removesuffix("USDT").removesuffix("USDC")
        base = re.sub(r"^(1000000|100000|10000|1000|1M)", "", base)
        if len(base) >= 2:
            out.add(base)
    return out


def leaks(text: str, terms: Iterable[str]) -> list[str]:
    found: list[str] = []
    upper = text.upper()
    for t in sorted(set(terms)):
        if re.search(rf"(?<![A-Z0-9]){re.escape(t)}(?![A-Z0-9])", upper):
            found.append(f"term:{t}")
    found += [f"date:{m.group(0)}" for m in _DATE.finditer(text)]
    found += [f"epoch:{m.group(0)}" for m in _EPOCH.finditer(text)]
    return found


def redact(text: str, terms: Iterable[str]) -> str:
    out = _EPOCH.sub("[time]", _DATE.sub("[date]", text))
    for t in sorted(set(terms), key=len, reverse=True):
        out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", "[asset]", out, flags=re.IGNORECASE)
    return out
