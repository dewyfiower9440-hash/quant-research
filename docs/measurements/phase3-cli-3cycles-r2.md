# Phase 3 Director 실행 측정 (tools/run_director.py)

> **정정 메모(2026-11-10, 수기):** 이 표는 비용 문구 수정 전 도구로 생성됐습니다. 이 실행은 가짜 LLM이 아니라 구독 CLI(`claude-code-cli`)이며 청구는 0입니다. `예상 $`와 `청구 가능 $` 열은 같은 토큰을 claude-opus-5-5 정가로 환산한 **추정치**이고 청구액이 아닙니다. 수치는 생성된 그대로입니다.
>
> (생성 당시 문구) 가짜 LLM은 비용이 들지 않습니다. 예상 비용은 같은 토큰을 claude-opus-5-5 정가로 환산한 값입니다.
> 이 결과는 자율 연구 능력의 증명이 아니고 전략 성과의 증거도 아닙니다.
> 목적은 연구 루프, 시도 회계, Memory 재사용, 자원 사용량을 확인하는 것입니다.

- 합계: `{"run_id": "cli-s7-on-posterior-20261110-r2", "llm": "claude-code-cli", "billed": false, "cycles": 3, "done": 3, "panel_load_s": 6.5, "wall_s": 132.6, "cpu_s": 21.0, "peak_private_mb": 683, "trials": 9, "done_duplicates": 0, "running_left": 0, "data_available": ["klines", "mark"], "billed_usd_total_all_runs": 0.0}`

| # | 상태 | 계열 | 경로 | trial | 중복 생략 | 원인 | LLM 호출 | 입력/출력 토큰 | 예상 $ | 청구 가능 $ | 시간 s | CPU s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | done | trend | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 2, "S3:done": 1} | 0 | missing_data, weak_signal, regime_dependent | 3 | 6/1529 | 0.486 | 0.0607 | 34.58 | 3.83 |
| 2 | done | momentum | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 2, "S3:done": 1} | 0 | missing_data, cost_erosion | 3 | 6/1683 | 0.4863 | 0.0486 | 44.97 | 4.66 |
| 3 | done | volatility | OBSERVE > HYPOTHESIZE > PLAN > GENERATE > SEARCH > BACKTEST > ANALYZE > LEARN > REPLAN > DONE | {"S2:done": 2, "S3:done": 1} | 0 | missing_data, cost_erosion, overfit | 3 | 6/1927 | 0.4865 | 0.0539 | 46.54 | 4.34 |
