# quant-research

- Install / sync: `uv sync`
- Run: `uv run quant-research`
- Test: `uv run pytest -q` (coverage gate 80%: `uv run pytest -q --cov`)
- Layout: `domain/` pure logic, `adapters/` I/O behind Protocols, `config.py` validated settings,
  `log.py` structlog setup, `errors.py` error hierarchy, `main.py` composition root
- Lint + format: `uv run ruff check --fix . && uv run ruff format .`
- Types: `uv run pyright`
