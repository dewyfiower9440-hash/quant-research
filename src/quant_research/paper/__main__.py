"""`python -m quant_research.paper`: the paper process with Polars limited to two threads.

Polars reads POLARS_MAX_THREADS once, at import. `quant_research.paper.run` imports Polars at module level,
so the limit is set here, before that import (measured A-1: 45-day bars over 231 lake files took 237 MB
private with two threads and about 1 GB with the default thread count)."""

from __future__ import annotations

import os


def main() -> None:
    os.environ.setdefault("POLARS_MAX_THREADS", "2")
    from quant_research.paper.run import main as run_main

    run_main()


if __name__ == "__main__":
    main()
