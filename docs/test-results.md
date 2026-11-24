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
| 2026-11-10 | Phase 2 문서 | b84efb7 | (재실행) | - | run 42671077766 1~3차 실패(Actions 장애, job 0개), 4차 success |
| 2026-11-10 | Phase 3 계획 | 6de82d7 | 선언 게이트 | 425 passed, 93.72% | run 42673623592 1차 실패(Actions 장애), 2차 success |
| 2026-11-10 | Phase 3-1·2 | 2485f96 | 선언 게이트(Memory, 익명화) | 445 passed, 93.89% | run 42675929109 success |
| 2026-11-10 | Phase 3-3~5 | ccbb0ef | 선언 게이트(스키마, 가짜 LLM, 예산, 상태 기계, 실패 분류, 정책) | 468 passed, 93.97% | run 42677695690 success |
| 2026-11-10 | Phase 3-6 | dd3a297 | 선언 게이트(Director, 재개, 중복, 격리) | 485 passed, 94.11% | run 42681391227 success |
| 2026-11-10 | Phase 3-8 | 74f38e0 | 선언 게이트(Claude 어댑터, anthropic==2.7.0) | 491 passed, 94.09% | run 42682986823 success |
| 2026-11-10 | Phase 3-7 | cda7ea2 | 선언 게이트(실행 도구, 정책 수정, 격자 확장) | 495 passed, 93.80% | run 42686111424 success |
| 2026-11-10 | Phase 3-7 | ffa0916 | 선언 게이트(쿨다운 해제, 게이트 테스트, 문서) | 498 passed, 94.06% | run 42705051505 success |
| 2026-11-10 | Phase 3-11·12 | 94c5c5f + 문서 | 선언 게이트(F-016 스키마 계약, 보고서 비용 문구) | 550 passed, 93.96% | 푸시 전(사용자 승인 대기) |
| 2026-11-10 | Phase 3-12 | 94c5c5f | 실제 LLM 3사이클 r2(구독 CLI) | LLM 9/9 ok, trial 9, 중복 0 | — |
| 2026-11-19 | F-017 수집기 heartbeat | b453600 | 선언 게이트(새 위치 C:/dev/auto/code) | 612 passed, 94.21% | run 44482809126: 1차 GitHub 결제 문제로 job 미시작 → 저장소 public 전환 후 재실행 success |
| 2026-11-19 | Phase 4-5 + 문서 | 5198c12, 9411169 | 선언 게이트(S2 승격 단계) | 615 passed, 94.21% | run 44486379011 success (두 커밋을 한 번에 push, head 9411169) |
| 2026-11-19 | Phase 4-7~9 | 0bcd293, 7822f6e, ff8709a | 선언 게이트(N_eff·DSR / TS bandit / MAP-Elites) | 638·644·651 passed | run 44491967830 / 44493023622 / (ff8709a: 측정표 §이력) success |
| 2026-11-19 | Phase 4-10a | 3bd0651 | 선언 게이트(드라이버·Director space 모드) | 655 passed, 94.15% | success (push 후 확인) |
| 2026-11-19 | Phase 5·6·7 | bd2efd8, 17050a4, 9da1aab | 선언 게이트(validator / paper·risk / live) | 705 passed, 92.91% | run 44512866500 success (head 9da1aab) |
| 2026-11-19 | Phase 4 마감 + 문서 | 6c2b24c | 선언 게이트(F-018, ONC 벡터화, run_search, 측정표) | 705 passed, 92.91% | run 44514181103 success |
| 2026-11-19 | 문서(계획 마감·Gotchas) | 3cf7d11 | 선언 게이트 | 705 passed, 92.91% | run 44517570761 success |
| 2026-11-19 | Phase 5 귀무 측정 | — | tools/run_null_pipeline.py none/random 100회, none/tpe 100회 | 통과 3/100, 5/100 (FWER 0.04, Wilson [0.020, 0.077]) | 측정표 docs/measurements/phase5-null-pipeline.md |
| 2026-11-19 | Phase 5 검정력 | — | tools/run_null_pipeline.py random·mechanism 알파, arm당 30회 | 회수율 0.067/0.033/0.000/0.033 (검정력 ≈ 0) | 측정표 docs/measurements/phase5-null-pipeline-alpha.md |
| 2026-11-20 | v2.2 A0 | 01f2f8c | 선언 게이트(설계 delta, ADR-040~048) | — | run 44622826560 success |
| 2026-11-20 | F-019·A1 | ac730ed…71c7260 | 선언 게이트(리뷰 1~4차 수정, 모의매매 피드 메모리) | — | run 44663936702 success (head 71c7260) |
| 2026-11-20 | v2.2 A2 | a28235a | 선언 게이트(판정 규칙 v2) | — | run 44669238136 success |
| 2026-11-20 | v2.2 A3 | 89dca8e | 선언 게이트(귀무·검정력 하네스 v2) | — | run 44674612084 success |
| 2026-11-20 | v2.2 A3 후속·A4 | 9bd3346, 94e76b3 | 선언 게이트(단순 수익률 중심화, bandit 보상 분리) | — | run 44678021445 success (head 94e76b3) |
| 2026-11-20 | v2.2 A6~A12 | e2d22aa, 3a785b5 | 선언 게이트 | — | run 44688270247 success (head 3a785b5) |
| 2026-11-20 | v2.2 B1·B2 | df49c67 | 선언 게이트(S1 최소 표본, 봉인 창) | 793 passed, 94.16% | run 44694783250 success |
| 2026-11-20 | v2.2 B2 2차 리뷰 | 6ef251e | 선언 게이트(창 끝 event 검사, 충실도 테스트) | 794 passed, 94.18% | run 44696488217 success |
| 2026-11-20 | v2.2 A3 부호 뒤집기 귀무 | 89dca8e | tools/run_null_v2.py --nulls signflip --reps 200 | 측정 159, 통과 0 (FWER 0, Wilson [0, 0.024]) | 측정표 docs/measurements/phase5-null-v2-signflip.md |
| 2026-11-22 | v2.3 단계 1 | 2059866 | 선언 게이트(ADR-049, 문서) | 928 passed | run 45016999953 success |
| 2026-11-22 | v2.3 단계 2 | b5ad029 | 선언 게이트(라운드 순수 핵심) | 940 passed | run 45018361713 success |
| 2026-11-22 | v2.3 단계 3 | ef398fe | 선언 게이트(러너 라운드·커버리지·도구) | 964 passed | run 45020811427 success |
| 2026-11-22 | v2.3 단계 5 | 60fa270 | 선언 게이트(데이터 큐) | 967 passed | run 45021864993 success |
| 2026-11-22 | v2.3 단계 4 | 13f420c | 선언 게이트(두 창 결합 확인) | 976 passed | run 45023861522 success |
| 2026-11-22 | v2.3 단계 6 | c2ca5e7 | 선언 게이트(비용 모형 v2) | 983 passed | run 45025289458 success |
| 2026-11-22 | v2.3 단계 6 측정 | c2ca5e7 | tools/measure_cost_model.py | 보류 대역 v1 0.687 → v2 0.795 (기준 0.9 미충족) | 측정표 docs/measurements/v23-cost-model.md |
| 2026-11-22 | v2.3 단계 7 | 6b63211 | 선언 게이트(metrics·spot 패널 열) | 990 passed | run 45027555190 success |
| 2026-11-22 | v2.3 단계 8 | 2f5fe3d | 선언 게이트(새 계열 3개) | 1003 passed | run 45029134822 success |
| 2026-11-22 | v2.3 단계 9 | bdd98f6 | 선언 게이트(validator 자동 제출) | 1006 passed | run 45030828572 success |
| 2026-11-22 | v2.3 단계 10 | 4612e13 | 선언 게이트 + Invoke-Pester tests/powershell(8/8, 게이트 밖) | 1006 passed | run 45032472251 success |
| 2026-11-23 | v2.3 단계 11 | aa0da41 | 선언 게이트(끝난 tier만 사용, c1 라운드 2 측정) | 1009 passed | run 45038382948 success |
| 2026-11-23 | v2.3 단계 12 | d5abc39 | 선언 게이트(최종 리뷰 수정: 유니버스 전체 tier, 커버리지 하한, 라운드 읽기 실패 일시 정지) | 1013 passed, 94.48% | run 45072161420 success |
| 2026-11-23 | v2.4 단계 1 | 4f0460f | 선언 게이트(설계 v2.3 정정본, ADR-050~055) | 1013 passed | run 45128403836 success |
| 2026-11-23 | v2.4 단계 2 | 701c904 | 선언 게이트(라벨 v3, c1 재집계) | 1023 passed | run 45130886823 success |
| 2026-11-23 | v2.4 단계 3·4 | ed67cb9 | 선언 게이트(S3 검정력 표, 공간 v3). 그 전 2회는 수집기 supervisor 타이밍 테스트 1건이 시스템 부하로 실패했고 다운로드를 멈춘 뒤 통과 | 1040 passed, 94.60% | run 45140949073 success |
| 2026-11-23 | v2.4 단계 5 | 10d7adf | 선언 게이트(펀딩 없는 선물 판정 금지) | 1046 passed, 94.62% | run 45143411789 success |
| 2026-11-24 | v2.4 단계 6·7 | f6e5c1d | 선언 게이트(결합 셀, Ridge 셀) | 1066 passed, 94.64% | run 45300573339 success |
| 2026-11-24 | v2.4 단계 8·9·10(코드) | db7b285 | 선언 게이트(귀무 측정, 국면 게이트, c2 라운드 순서) + Invoke-Pester tests/powershell(10/10, 게이트 밖) | 1090 passed, 94.69% | run 45311960895 success |
| 2026-11-24 | v2.4 단계 10·11 | d453da4 | 선언 게이트(c2 첫 틱 측정, 전체 범위 리뷰의 주석·문서 정리) | 1090 passed, 94.69% | run 45338914925 success |
