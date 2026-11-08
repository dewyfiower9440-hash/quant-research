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

## Project map
- Design (v2.1, reference): `docs/plans/2026-11-08-autonomous-quant-research.md`
- Execution plan (in progress, approved Phase 0-1 only): `docs/plans/2026-11-08-phase0-1-build.md`
- Living records: `docs/progress.md` (per-task status), `docs/decisions.md` (ADR), `docs/failures.md`
  (errors, repro, root cause, attempts), `docs/test-results.md`, `docs/measurements/`
- Data lives OUTSIDE the repo: `C:\dev\qr-data` (raw JSONL, lake, meta SQLite, backups). Never commit data.
- Never modify `C:\Project` (old stack, reference only), VMware VMs or other projects.

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
