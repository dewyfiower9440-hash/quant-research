# Phase 3 Director 실행 측정 (tools/run_director.py)

> 가짜 LLM은 비용이 들지 않습니다. 예상 비용은 같은 토큰을 claude-opus-5-5 정가로 환산한 값입니다.
> 이 결과는 자율 연구 능력의 증명이 아니고 전략 성과의 증거도 아닙니다.
> 목적은 연구 루프, 시도 회계, Memory 재사용, 자원 사용량을 확인하는 것입니다.

- 합계: `{"run_id": "fake-s7-on-posterior-20261109T194855", "llm": "fake-llm-v1", "billed": false, "cycles": 10, "done": 10, "panel_load_s": 4.6, "wall_s": 38.0, "cpu_s": 40.2, "peak_private_mb": 813, "trials": 24, "done_duplicates": 0, "running_left": 0, "data_available": ["klines", "mark"], "billed_usd_total_all_runs": 0.0}`

| # | 상태 | 계열 | 경로 | trial | 중복 생략 | 원인 | LLM 호출 | 입력/출력 토큰 | 예상 $ | 청구 가능 $ | 시간 s | CPU s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > VALIDATE > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 1, "S3:done": 1} | 0 | missing_data | 3 | 827/168 | 0.4833 | 0.0067 | 3.02 | 3.11 |
| 2 | done | volatility | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 2, "S3:done": 1} | 0 | missing_data, weak_signal | 3 | 948/180 | 0.4838 | 0.0074 | 3.99 | 4.14 |
| 3 | done | momentum | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > VALIDATE > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 2, "S3:done": 1} | 0 | missing_data, regime_dependent | 3 | 994/179 | 0.484 | 0.0076 | 4.22 | 4.58 |
| 4 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 1, "S3:done": 1} | 0 | missing_data, weak_signal | 3 | 955/173 | 0.4838 | 0.0073 | 2.78 | 2.78 |
| 5 | done | mean_reversion | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 1, "S3:done": 1} | 0 | missing_data, cost_erosion | 3 | 978/183 | 0.4839 | 0.0076 | 2.72 | 2.75 |
| 6 | done | mean_reversion | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 2, "S3:done": 1} | 0 | missing_data, cost_erosion | 3 | 1098/184 | 0.4844 | 0.0081 | 3.98 | 4.08 |
| 7 | done | mean_reversion | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 1, "S3:done": 1} | 0 | missing_data, cost_erosion | 3 | 1035/183 | 0.4841 | 0.0078 | 2.74 | 2.78 |
| 8 | done | volatility | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 1, "S3:done": 1} | 0 | missing_data, weak_signal | 3 | 1020/179 | 0.4841 | 0.0077 | 2.7 | 2.83 |
| 9 | done | momentum | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > VALIDATE > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 2, "S3:done": 1} | 0 | missing_data, regime_dependent | 3 | 1083/179 | 0.4843 | 0.0079 | 4.36 | 4.61 |
| 10 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 1, "S3:done": 1} | 0 | missing_data, cost_erosion, regime_dependent | 3 | 993/180 | 0.484 | 0.0076 | 2.87 | 2.88 |
