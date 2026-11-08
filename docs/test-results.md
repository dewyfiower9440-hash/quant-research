# 테스트·게이트·CI 결과

형식: 시각, 작업 번호, 커밋, 명령, 결과(통과/실패 수, 커버리지), CI run id.
실패도 그대로 기록하고, 원인은 `docs/failures.md`에 연결합니다.

| 시각 | 작업 | 커밋 | 명령 | 결과 | CI |
|---|---|---|---|---|---|
| 2026-11-08 18:4x KST | 1 | 923b1fe | 선언 게이트(ruff, format, pyright, pytest --cov) | 14 passed, 커버리지 93.06%, pyright 0 errors | run 42468357091 success(job check, pip-audit 포함) |
| 2026-11-08 18:5x KST | 2 | (커밋 전) | `tools/phase0_probe.py` 1차 실행 | **실패**: Polars DuplicateError(F-002) | — |
| 2026-11-08 18:5x KST | 2 | (커밋 전) | `tools/phase0_probe.py --ws-seconds 30` 재실행 | 성공, JSON 생성 | — |
| 2026-11-08 19:0x KST | 2 | (커밋 전) | 선언 게이트 | 31 passed, 커버리지 95.87%, pyright 0 errors | run 42469337357 success (커밋 ab599af) |
