# Phase 3 Director 실행 측정 (tools/run_director.py)

> LLM `claude-code-cli`: API 청구가 없습니다(가짜 LLM 또는 구독 CLI). `예상 $`와 `사용량 $`는 같은 토큰을 claude-opus-5-5 정가로 환산한 **추정치**이며 청구액이 아닙니다.
> 이 결과는 자율 연구 능력의 증명이 아니고 전략 성과의 증거도 아닙니다.
> 목적은 연구 루프, 시도 회계, Memory 재사용, 자원 사용량을 확인하는 것입니다.

- 합계: `{"run_id": "director-space-cli-20261120", "llm": "claude-code-cli", "billed": false, "cycles": 3, "done": 3, "panel_load_s": 3.0, "wall_s": 93.4, "cpu_s": 22.7, "peak_private_mb": 880, "trials": 259, "done_duplicates": 188, "running_left": 0, "data_available": ["klines", "mark"], "billed_usd_total_all_runs": 0.0}`

| # | 상태 | 계열 | 경로 | trial | 중복 생략 | 원인 | LLM 호출 | 입력/출력 토큰 | 예상 $ | 사용량 $ | 시간 s | CPU s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > VALIDATE > ANALYZE > LEARN > REPLAN > DONE | {"S0:done": 38, "S0:rejected": 2, "S1:done": 38, "S2:done": 6, "S3:done": 1} | 0 | missing_data | 2 | 4/1350 | 0.326 | 0.0513 | 31.09 | 5.91 |
| 2 | done | volatility | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S0:done": 39, "S0:rejected": 1, "S1:done": 39, "S2:done": 6, "S3:done": 1} | 0 | missing_data, cost_erosion, overfit, regime_dependent | 2 | 4/1386 | 0.3262 | 0.0404 | 29.03 | 5.66 |
| 3 | done | momentum | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > VALIDATE > ANALYZE > LEARN > REPLAN > DONE | {"S0:done": 40, "S1:done": 40, "S2:done": 7, "S3:done": 1} | 0 | missing_data | 2 | 4/1409 | 0.3267 | 0.0418 | 30.26 | 7.39 |
