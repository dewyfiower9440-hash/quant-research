"""data.binance.vision S3 listing and universe month selection, shared by the bulk runners."""

from __future__ import annotations

import re
from collections.abc import Sequence

import httpx

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"


def listing(client: httpx.Client, prefix: str) -> list[tuple[str, int, str]]:
    """(key, size, LastModified) of every .zip under `prefix`, following S3 pagination."""
    out: list[tuple[str, int, str]] = []
    marker = ""
    while True:
        r = client.get(S3, params={"prefix": prefix, "marker": marker})
        r.raise_for_status()
        for block in re.findall(r"<Contents>(.*?)</Contents>", r.text, flags=re.S):
            key = re.search(r"<Key>([^<]+)</Key>", block)
            size = re.search(r"<Size>(\d+)</Size>", block)
            mod = re.search(r"<LastModified>([^<]+)</LastModified>", block)
            if key and size and mod and key.group(1).endswith(".zip"):
                out.append((key.group(1), int(size.group(1)), mod.group(1)))
        if "<IsTruncated>true</IsTruncated>" not in r.text:
            return out
        marker = out[-1][0]


def wanted_months(spans: Sequence[Sequence[str | None]], months: Sequence[str], warmup: int) -> set[str]:
    """Months of the membership spans (end exclusive), each extended `warmup` months back."""
    wanted: set[str] = set()
    for start, end in spans:
        lo = list(months).index(str(start)) - warmup
        hi = list(months).index(end) if end is not None else len(months)
        wanted |= set(months[max(0, lo) : hi])
    return wanted
