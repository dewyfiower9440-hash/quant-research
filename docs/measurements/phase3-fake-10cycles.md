# Phase 3 Director 실행 측정 (tools/run_director.py)

> 가짜 LLM은 비용이 들지 않습니다. 예상 비용은 같은 토큰을 claude-opus-5-5 정가로 환산한 값입니다.
> 이 결과는 자율 연구 능력의 증명이 아니고 전략 성과의 증거도 아닙니다.
> 목적은 연구 루프, 시도 회계, Memory 재사용, 자원 사용량을 확인하는 것입니다.

- 합계: `{"run_id": "fake-s7-on-posterior-20261109T192937", "llm": "fake-llm-v1", "billed": false, "cycles": 10, "done": 10, "panel_load_s": 5.1, "wall_s": 17.1, "cpu_s": 19.1, "peak_private_mb": 838, "trials": 10, "done_duplicates": 0, "running_left": 0, "data_available": ["klines", "mark"], "billed_usd_total_all_runs": 0.0}`

| # | 상태 | 계열 | 경로 | trial | 중복 생략 | 원인 | LLM 호출 | 입력/출력 토큰 | 예상 $ | 청구 가능 $ | 시간 s | CPU s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > VALIDATE > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 1, "S3:done": 1} | 0 | missing_data | 3 | 681/168 | 0.4827 | 0.0061 | 2.34 | 2.41 |
| 2 | done | volatility | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 2, "S3:done": 1} | 0 | missing_data, weak_signal | 3 | 814/180 | 0.4833 | 0.0069 | 3.31 | 3.45 |
| 3 | done | momentum | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 2, "S3:done": 1} | 0 | missing_data, cost_erosion | 3 | 888/178 | 0.4836 | 0.0071 | 3.69 | 4.06 |
| 4 | done | momentum | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 630/121 | 0.3225 | 0.0049 | 0.12 | 0.09 |
| 5 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 616/119 | 0.3225 | 0.0048 | 0.12 | 0.12 |
| 6 | done | mean_reversion | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 1, "S3:done": 1} | 0 | missing_data, cost_erosion, regime_dependent | 3 | 896/189 | 0.4836 | 0.0074 | 1.93 | 2.06 |
| 7 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 689/117 | 0.3228 | 0.0051 | 0.12 | 0.11 |
| 8 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 677/117 | 0.3227 | 0.005 | 0.13 | 0.12 |
| 9 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 665/117 | 0.3227 | 0.005 | 0.12 | 0.11 |
| 10 | done | volatility | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 674/125 | 0.3227 | 0.0052 | 0.1 | 0.09 |
