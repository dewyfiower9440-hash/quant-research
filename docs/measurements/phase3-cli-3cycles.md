# Phase 3 Director 실행 측정 (tools/run_director.py)

> 가짜 LLM은 비용이 들지 않습니다. 예상 비용은 같은 토큰을 claude-opus-5-5 정가로 환산한 값입니다.
> 이 결과는 자율 연구 능력의 증명이 아니고 전략 성과의 증거도 아닙니다.
> 목적은 연구 루프, 시도 회계, Memory 재사용, 자원 사용량을 확인하는 것입니다.

- 합계: `{"run_id": "cli-s7-on-posterior-20261110", "llm": "claude-code-cli", "billed": false, "cycles": 3, "done": 3, "panel_load_s": 10.7, "wall_s": 83.3, "cpu_s": 10.9, "peak_private_mb": 793, "trials": 0, "done_duplicates": 0, "running_left": 0, "data_available": ["klines", "mark"], "billed_usd_total_all_runs": 0.0}`

| # | 상태 | 계열 | 경로 | trial | 중복 생략 | 원인 | LLM 호출 | 입력/출력 토큰 | 예상 $ | 청구 가능 $ | 시간 s | CPU s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | done | None | OBSERVE > HYPOTHESIZE > REPLAN > DONE | {} | 0 |  | 2 | 4/1514 | 0.3236 | 0.0447 | 24.02 | 0.94 |
| 2 | done | None | OBSERVE > HYPOTHESIZE > REPLAN > DONE | {} | 0 |  | 2 | 4/1574 | 0.3236 | 0.0424 | 24.45 | 0.42 |
| 3 | done | None | OBSERVE > HYPOTHESIZE > REPLAN > DONE | {} | 0 |  | 2 | 4/1520 | 0.3236 | 0.0413 | 24.15 | 0.45 |
