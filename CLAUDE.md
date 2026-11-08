# quant-research

Stack: Python (uv)

## Commands
- Install / sync: `uv sync`
- Run: `uv run quant-research`
- Test: `uv run pytest -q` (coverage gate 80%: `uv run pytest -q --cov`)
- Layout: `domain/` pure logic, `adapters/` I/O behind Protocols, `config.py` validated settings,
  `log.py` structlog setup, `errors.py` error hierarchy, `main.py` composition root
- Lint + format: `uv run ruff check --fix . && uv run ruff format .`
- Types: `uv run pyright`

## Verification
<!-- Read by the discipline hook: the declared gate must pass (run verbatim, from this
     directory, in the foreground) and, with ci: required, the pushed commit's CI must be green. -->
- gate: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q --cov`
- ci: required

## Conventions
- Follow the global rules in ~/.claude/CLAUDE.md (verify before "done", test-first bug fixes, minimal scope).
- Keep this file updated when commands or architecture change.

## Plans
- Multi-step work is planned with `/plan` and saved as `docs/plans/YYYY-MM-DD-<slug>.md`
  (frontmatter `status: draft|in-progress|done`, steps as `- [ ] N. ...` checkboxes).
- `/implement` ticks the boxes and commits per step; after a compaction or a new session,
  re-read the in-progress plan and resume from the first unchecked step.

## Gotchas
<!-- One line per verified lesson: `- <trap> → <what to do> (YYYY-MM-DD)`. Propose a cleanup past 40 lines. -->
