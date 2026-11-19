# Phase 5 파이프라인 귀무 검증·검정력 (tools/run_null_pipeline.py)

> 신호 없는 합성 시장(실데이터 수익률의 블록 부트스트랩)에서 탐색(S0~S3)부터 holdout 판정까지 돌린 결과.
> `none` 행의 통과율 = 종단 FWER 추정치(목표 0.05, ADR-022). `random`·`mechanism` 행 = 심은 알파의 회수율.
> 모든 trial은 임시 research.db에 기록됐고 운영 research.db에는 쓰지 않았습니다.
> 실전 전략의 성과 증거가 아닙니다.

- 설정: `{"reps": 100, "arms": ["tpe"], "alphas": ["none"], "rule_hash": "db2b7bdb10b0c245478191144953974e04c4a643e4b70705e2d177a8fa74faff", "alpha_per_query": 0.0025, "panel_load_s": 3.1, "wall_s": 481.4, "peak_private_mb": 862}`

| 알파 | 강도 | 탐색기 | 반복 | 통과율 | Wilson 95% | 탐색기별 |
|---|---|---|---|---|---|---|
| none | 0.0 | tpe | 100 | 0.050 | [0.022, 0.112] | {"tpe": 0.05} |

## 판정
- FWER(`none` 행)의 Wilson 95% 상한이 0.05 아래면 목표 충족, 아니면 **미충족으로 그대로 보고**.
  기준 조정은 사전 등록으로만 합니다.
- 검정력(`random`·`mechanism` 행)은 8심볼·6개월 합성 시장 기준이라 낮을 수 있습니다. 표본이 늘면 다시 측정.
- Director(가짜 LLM) 비교는 같은 합성 패널에서 별도 측정합니다(`--arms director`는 이 도구에서 미구현 표시).
