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
- Design (v2.1 base + v2.2 / v2.3 deltas, reference): `docs/plans/2026-11-08-autonomous-quant-research.md`,
  `docs/plans/2026-11-20-autonomous-quant-research-v2.2.md`, `docs/plans/2026-11-23-autonomous-quant-research-v2.3.md` (ADR-040..055)
- Execution plans: `docs/plans/2026-11-23-v24-feasible-rerun.md` (v2.4: executable spaces v3, funding required,
  model cells, regime gates, campaign c2); done: `2026-11-22-v23-never-stop.md`, `2026-11-20-v22-rebuild.md`
- v2.4 code: `strategy/space.py` (`SPACES_V3` = `family@tf/v3` with 90-day lookback caps, `MODEL_SPACES`,
  `REGIME_SPACES`), `strategy/templates.py` (`constituent`, `build_model`, `regime_gate`), `search/model_cells.py`
  (combination and ridge weights fitted on the first 75 % of selection), `search/campaign.py`
  (`preregistered_groups_v3`, causes `below_bar` / `infeasible`); tools `relabel_campaign.py`,
  `measure_combined_null.py`; campaign c2 = `run_campaign.py --run-id c2 --space-version 3`
- Autonomous loop (v2.3, ADR-049): `search/campaign.py` (cells, causes, rounds), `search/campaign_runner.py` (driver,
  cross-window confirmation), `catalog/coverage.py`, `bulk/tiers.py` (data queue planning); tools `run_campaign.py`,
  `run_data_queue.py`, `run_validator.py`; user-run tasks `tools/register_campaign_task.ps1`,
  `register_data_queue_task.ps1`, `register_validator_task.ps1` (`docs/operations.md` section 18). State outside the
  repo: `qr-data/run/campaign-c1.json`, `data-queue.json`, `data-queue-state.json`, report `qr-data/run/campaign-c1.md`
- PowerShell tests are outside the gate and CI: `Invoke-Pester -Path tests/powershell` (the scripts only with `-WhatIf`)
- Execution plan (in progress, approved Phase 0-1 only): `docs/plans/2026-11-08-phase0-1-build.md`
- Phase 2 plan (DSL, two engines, cost model, trial accounting): `docs/plans/2026-11-09-phase2-dsl-backtest.md`;
  code in `strategy/` (spec, ops, canonical, s0, compile, templates), `backtest/` (panel, costs, market, engines,
  run), `research/` (research.db, trial ledger); smoke `tools/run_backtest.py`. Every evaluation goes through
  `TrialLedger.evaluate` (one trial per evaluation, refusals included).
- Phase 3 (Director, memory, LLM): `docs/plans/2026-11-10-phase3-director-memory.md`; code in `director/`, `research/memory.py`
- Phase 4 (search): `docs/plans/2026-11-10-phase4-search.md` (steps 1-6) and `docs/plans/2026-11-19-phase4-7-build.md`
  (steps 7-11 and Phases 5-7); code in `strategy/space.py` (pre-registered space), `search/` (s0/s1/s2 stages,
  promotion, cost model, optimizers = Random/TPE on optuna, multiplicity = ONC/DSR, bandit, archive = MAP-Elites,
  pipeline driver); Director `search="space"` mode; real-data batches via `tools/run_search.py`
- Phase 5 (validator): `validator/` (store = validator.db with budget k / 1-bit verdicts / hash-chained access log,
  verdict rule, Romano-Wolf StepM, null pipeline harness, lifecycle + degradation rules, prior-knowledge checks);
  `tools/run_null_pipeline.py`; research outbox `research/migrations/0003_validation.sql`
- Phase 6 (paper): `paper/` (ledger.db hash-chained journal, quote-sample fill engine, feed, divergence, monitor,
  `paper/run.py` process), `risk/` (limits, signed policy); `tools/register_paper_task.ps1` (user-run)
- Phase 7 (live preparation, laptop side only): `live/` (ed25519 signing, order rules, dead-man, testnet request
  formats); `docs/live-rehearsal.md` lists the user-only decisions. No keys, no orders from this machine.
- Phase 1 status: IMPLEMENTED / OPERATIONAL_VALIDATION_PENDING; carry-over issues OPS-001..008 in `docs/operations.md`
- Environment versions (separate from the code version): `docs/environment.md`
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
- `gh run list -L 1` right after a push returned the "Dependabot Updates" run for the same SHA, not CI → filter `event == push` and `workflowName == ci` before citing a run id (2026-11-08)
- The editor format hook runs ruff from outside the project and treated `quant_research` as third-party (stripping the import-group blank line) while the gate wanted it first-party → keep `[tool.ruff.lint.isort] known-first-party` in pyproject (2026-11-08)
- The format hook's `ruff --fix` deletes imports that are unused at the moment of an intermediate edit → add the import in the same edit as its first use, or re-check imports after multi-edit sequences (2026-11-08)
- The scaffold .gitignore line `data/` ignored every `data/` folder, so src/quant_research/data and tests/data were never committed while the local gate passed (F-007) -> keep `/data/` (root only) and check `git status --porcelain --ignored` for ignored sources before calling a step done (2026-11-09)
- The scaffold refuses a non-empty target and deleting the workspace dir is blocked by a safety check → move existing files aside, scaffold into the empty dir, move them back (2026-11-08)
- `python -I` ignores PYTHONIOENCODING, so printing a Polars frame (box-drawing chars) raises UnicodeEncodeError on the cp949 console → print `.rows()` / JSON in scripts (2026-11-09)
- GNU sed turns `\n` in a replacement into a real newline, and `Ns/...` line addresses go stale once the format hook reflows a file (both corrupted test_extras.py) → use Edit for literal multi-char edits; sed only for single-line patterns without escapes (2026-11-09)
- Registering a scheduled task (Register-ScheduledTask via a tools/*.ps1) is refused by the auto-mode classifier as persistence → write the script, dry-run it with `-WhatIf`, and hand the user the `! powershell -NoProfile -File ...` command (2026-11-09)
- pyright strict reports `np.sum` / `np.where` results as Unknown → use `np.dot`, `np.count_nonzero` and wrap
  `np.where` in `np.asarray(..., dtype=np.float64)` with an `F64` annotation (2026-11-10)
- The gate's pyright only covers `src` and `tests` (`[tool.pyright] include`); `tools/*.py` are not type-checked by the gate → run `uv run pyright tools/<file>.py` for every tool you touch (2026-11-09)
- `! powershell -File C:\dev\...` typed by the user runs in Git Bash, which eats the backslashes (`C:devauto...`) → hand the user forward-slash paths (`C:/dev/auto/code/tools/x.ps1`) (2026-11-19)
- `Stop-ScheduledTask qr-collector-supervisor` alone is undone within seconds by the task's restart settings → `Disable-ScheduledTask` first; re-registering with `-Force` re-enables it (2026-11-19)
- A 0-byte `run/ws.heartbeat.json` left by a crash made every ws restart die for 7 days with only one toast (F-017) → after any collector redeploy check `logs/supervisor.log` for repeating `child_exited` (2026-11-19)
- Piping the gate's pytest through `| tail` hides its exit status from the verification hook ("다른 테스트 명령만 실행됨") → run the declared gate verbatim, unpiped, before every commit (2026-11-19)
- ONC clustering (`search.multiplicity.onc_clusters`) over ~300 trials took tens of minutes with the python-loop silhouette → it is vectorised and capped at k <= 30; pass `max_k=None` only for small groups (2026-11-19)
- `Path.write_text` on Windows writes CRLF (text mode) and the repository is LF (`.gitattributes eol=lf`) -> patch scripts write with `write_bytes(s.encode("utf-8"))` (2026-11-20)
- Editing an approved plan file beyond ticking checkboxes (e.g. appending to its progress log) revokes the approval ("유효한 v2 승인 없음") -> keep the plan file verbatim apart from `[x]`, record progress in `docs/progress.md` (2026-11-20)
- A paper tick test whose strategy never trades still passes every `if open_orders:` block -> assert the precondition (`assert ledger.open_orders()`), and give the fixture enough history for the feature warm-up plus the decision window (2026-11-20)
- `months_between(first_ns, end_ns - 1)` included the month after the window: `datetime.fromtimestamp(ns / 1e9)` rounds ns - 1 back up to the boundary → subtract a whole second (`end_ns - 10**9`) for an exclusive end (2026-11-20)
- A compound command that the commit hook blocks runs nothing: a message-file write chained before `git commit -F` was never written, so the rerun failed with "could not read log file" → write the commit message file in its own call (Write tool) before committing (2026-11-20)
- A per-row join of (symbol, day) with all of a symbol's days looked fine on 6-month panels and took 4.4 GB on a 36-month 1d panel (F-020) → measure memory of every per-day aggregation on the longest window before a campaign; prefer cumulative sums + as-of joins over cross joins (2026-11-21)
- Binance lists symbols with CJK names (`币安人生USDT`); printing them to the cp949 console killed `tools/bulk_extras.py` at symbol 629/632 → job CLIs print through `adapters.jobhost.Log` (escapes what the console cannot encode); for one-off scripts set `PYTHONIOENCODING=utf-8` (not with `python -I`) (2026-11-21)
- `sed` with a Windows path in the replacement turned `run\campaign` into a control character (`\c`) inside a .ps1 → never put backslashes through sed; use Edit, or a Python script that replaces exact bytes (2026-11-22)
- A scheduled campaign tick wrote its report into `docs/measurements/` and would have rewritten a committed file every hour → scheduled runs write under `qr-data/run/`; copy a report into docs/ only to commit it (2026-11-22)
- A dataset counted as available as soon as one file existed, so a campaign cell ran on a tier that was 20 % downloaded → research uses a tier only when `run/data-queue-state.json` says `done`, and an input must cover 80 % of the member rows (2026-11-23)
- `--top N` in `tools/bulk_extras.py` ranks symbols by member months up to today (look-ahead, 41 % of a 2025 month's universe) → tiers a family reads download the whole universe file, never a top-N of it (2026-11-23)
- The supervisor restart test (`tests/capture/test_run_orchestration.py`) failed the gate twice while a download tick and Defender were busy (child start took 10-16 s instead of 1 s); it passes alone → when the gate fails only there, stop the `qr-data-queue` tick (and its orphaned `bulk_extras.py` children) and rerun; do not run other pytest jobs beside the gate (2026-11-23)
- pyright infers `list[str]` for a list built from `Literal`-returning calls, and `x in set_of_tuples` narrows a loop variable to the set's element type → annotate the list (`nulls: list[NullKind] = [...]`) and compare with an explicit key (2026-11-24)
- A polars join does not promise the left frame's row order: values from `evaluate(order(frame), ...)` attached after a join can be misaligned → attach them to the ordered frame first, then join (2026-11-24)
- A model-cell or S1 fixture with a constant funding rate / premium per symbol has no rolling variance, so `zscore` is null and every combined signal (any null term) disappears → give test inputs movement over time (2026-11-24)
- A long measurement beside pytest runs took 4 minutes per replication instead of 35 seconds → run resumable measurements alone, with a JSON-lines state file (2026-11-24)
- structlog's JSON tracebacks print frame locals by default → `configure_logging(show_locals=False)` wherever locals can hold holdout results (the validator) (2026-11-22)
- Background shells in a Claude Code session are killed when the whole system runs low on memory (bulk funding download stopped at 280/632) → run long jobs in the foreground (20 min per call, resumable tools) or register a scheduled task (`tools/register_campaign_task.ps1`, user-run) (2026-11-21)
