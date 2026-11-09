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
| 2026-11-09 22:2x KST | 마무리 1 | d6dcdc8 | 선언 게이트(카탈로그 스냅샷) | 240 passed, 92.67% | run 42624331261 success |
| 2026-11-09 22:3x KST | 마무리 2 | e8c09bc | 선언 게이트(모니터, basis) | 261 passed, 92.73% | run 42626757724 success |
| 2026-11-09 22:4x KST | 마무리 2 | 223acb1 | 선언 게이트(F-012 gap 가져오기) | 262 passed, 92.88% | run 42627623060 success |
| 2026-11-09 22:4x KST | 마무리 4b | 3d37186 | 선언 게이트(상품·유니버스) | 271 passed, 93.06% | run 42628316477 success |
| 2026-11-09 22:5x KST | 마무리 4 | 741e3f1 | 선언 게이트(T1 추가 벌크) | 280 passed, 93.17% | run 42630262427 success |
| 2026-11-09 23:0x KST | 마무리 5 | 1d0c690 | 선언 게이트(REST 대조) | 285 passed, 93.21% | run 42632224371 success |
| 2026-11-09 23:1x KST | 마무리 6 | 324db1e | 선언 게이트(7일 검증 도구) | 289 passed, 93.29% | run 42632913154 success |
| 2026-11-09 23:2x KST | 마무리 7 | 2f455a1 | 선언 게이트(문서) | 289 passed, 93.29% | run 42633563200 success |
| 2026-11-10 01:2x KST | Phase 2 계획 | 22e39f2 | 선언 게이트(버전 0.2.0) | 289 passed, 93.29% | run 42637257564 success |
| 2026-11-10 01:4x KST | Phase 2-1 | a3fb100 | 선언 게이트(research.db, 시도 회계) | 297 passed, 93.17% | run 42638918593 success |
| 2026-11-10 02:0x KST | Phase 2-2 | f5f8fb4 | 선언 게이트(DSL, S0) | 333 passed, 93.48% | run 42641372698 success |
| 2026-11-10 02:2x KST | Phase 2-3 | c8a4860 | 선언 게이트(패널) | 341 passed, 93.13% | run 42642667952 success |
| 2026-11-10 02:3x KST | Phase 2-4 | 89012c6 | 선언 게이트(연산자 누수) | 399 passed, 93.13% | run 42643589847 success |
| 2026-11-10 02:4x KST | Phase 2-5 | 7a5917d | 선언 게이트(비용 모델) | 407 passed, 93.23% | run 42644112134 success |
| 2026-11-10 03:0x KST | Phase 2-6·7 | 47a85b0 | 선언 게이트(두 엔진, 기준 사례, 교차 300) | 415 passed, 91.58% | run 42646002172 success |
| 2026-11-10 02:3x KST | Phase 2-8 | 7d89e8c | 선언 게이트(실행기, 템플릿) | 425 passed, 93.75% | run 42667971088 success |
| 2026-11-10 02:5x KST | Phase 2-9 | 6f420ed | 선언 게이트(스모크, 심볼 단위 적재) | 425 passed, 93.72% | run 42669255810 success |
| 2026-11-10 03:1x KST | Phase 2-10 | 1a2c533 | 선언 게이트(문서) | 425 passed, 93.72% | run 42670010290 success |
