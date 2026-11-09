# 테스트·게이트·CI 결과

형식: 시각, 작업 번호, 커밋, 명령, 결과(통과/실패 수, 커버리지), CI run id.
실패도 그대로 기록하고, 원인은 `docs/failures.md`에 연결합니다.

| 시각 | 작업 | 커밋 | 명령 | 결과 | CI |
|---|---|---|---|---|---|
| 2026-11-08 18:4x KST | 1 | 923b1fe | 선언 게이트(ruff, format, pyright, pytest --cov) | 14 passed, 커버리지 93.06%, pyright 0 errors | run 42468357091 success(job check, pip-audit 포함) |
| 2026-11-08 18:5x KST | 2 | (커밋 전) | `tools/phase0_probe.py` 1차 실행 | **실패**: Polars DuplicateError(F-002) | — |
| 2026-11-08 18:5x KST | 2 | (커밋 전) | `tools/phase0_probe.py --ws-seconds 30` 재실행 | 성공, JSON 생성 | — |
| 2026-11-08 19:0x KST | 2 | (커밋 전) | 선언 게이트 | 31 passed, 커버리지 95.87%, pyright 0 errors | run 42469337357 success (커밋 ab599af) |
| 2026-11-08 19:3x KST | 3 | aaba1ac | 선언 게이트 | 80 passed, 92.14% | run 42471093283 success |
| 2026-11-08 19:5x KST | 3 | 19e067c | 선언 게이트(F-004) | 81 passed, 92.21% | run 42471562536 success |
| 2026-11-08 20:2x KST | 3 | d57e9df | 선언 게이트(F-005, 리뷰 11건) | 97 passed, 91.49% | run 42472784613 success |
| 2026-11-08 20:3x KST | 3 | aaba1ac | 1시간 실측(supervisor --duration 3600) | bookTicker 3597/3600, Upbit 호가 3598, forceOrder 2998, 작업 오류 1(basis ANTHROPICUSDT Invalid pair), 원본 1.45GB | — |
| 2026-11-08 20:5x KST | 3 | 6b07d7a | 선언 게이트(ADR-006/007) | 106 passed, 91.21% | run 42477750110 success |
| 2026-11-08 21:1x KST | 3 | 39e5a94 | 선언 게이트(supervisor STOP) | 107 passed, 91.25% | run 42480240871 success |
| 2026-11-09 11:2x KST | 리뷰 수정 | 5312f2b | 선언 게이트(F-010 A·B·C) | 211 passed, 92.61% | run 42550683502 success |
| 2026-11-09 11:3x KST | 리뷰 수정 | 7353810 | 선언 게이트(F-010 재리뷰 4건) | 215 passed, 92.58% | run 42551635899 success |
| 2026-11-09 17:2x KST | 10a | 8fe7b2e | 선언 게이트(F-011 수신 공백, 백업 경쟁) | 235 passed, 92.59% | run 42585795643 success |
