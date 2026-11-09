# Phase 3 Director 실행 측정 (tools/run_director.py)

> 가짜 LLM은 비용이 들지 않습니다. 예상 비용은 같은 토큰을 claude-opus-5-5 정가로 환산한 값입니다.
> 이 결과는 자율 연구 능력의 증명이 아니고 전략 성과의 증거도 아닙니다.
> 목적은 연구 루프, 시도 회계, Memory 재사용, 자원 사용량을 확인하는 것입니다.

- 합계: `{"run_id": "fake-s7-on-posterior-20261109T193213", "llm": "fake-llm-v1", "billed": false, "cycles": 10, "done": 10, "panel_load_s": 4.9, "wall_s": 10.9, "cpu_s": 11.5, "peak_private_mb": 827, "trials": 0, "done_duplicates": 0, "running_left": 0, "data_available": ["klines", "mark"], "billed_usd_total_all_runs": 0.0}`

| # | 상태 | 계열 | 경로 | trial | 중복 생략 | 원인 | LLM 호출 | 입력/출력 토큰 | 예상 $ | 청구 가능 $ | 시간 s | CPU s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | done | volatility | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 513/123 | 0.3221 | 0.0045 | 0.63 | 0.56 |
| 2 | done | mean_reversion | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 545/126 | 0.3222 | 0.0047 | 0.53 | 0.53 |
| 3 | done | funding_basis | OBSERVE > HYPOTHESIZE > PLAN > ACQUIRE_DATA > REPLAN > DONE | {} | 0 | missing_data | 2 | 556/129 | 0.3222 | 0.0048 | 0.62 | 0.56 |
| 4 | done | momentum | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 614/121 | 0.3225 | 0.0049 | 0.61 | 0.52 |
| 5 | done | volatility | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 633/124 | 0.3225 | 0.005 | 0.52 | 0.5 |
| 6 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 625/118 | 0.3225 | 0.0049 | 0.64 | 0.58 |
| 7 | done | funding_basis | OBSERVE > HYPOTHESIZE > PLAN > ACQUIRE_DATA > REPLAN > DONE | {} | 0 | missing_data | 2 | 648/129 | 0.3226 | 0.0052 | 0.57 | 0.47 |
| 8 | done | momentum | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 663/120 | 0.3227 | 0.0051 | 0.6 | 0.53 |
| 9 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 649/117 | 0.3226 | 0.0049 | 0.62 | 0.56 |
| 10 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > REPLAN > DONE | {} | 0 |  | 2 | 649/117 | 0.3226 | 0.0049 | 0.61 | 0.55 |
