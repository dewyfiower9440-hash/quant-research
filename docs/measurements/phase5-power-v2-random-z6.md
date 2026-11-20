# Phase 5 종단 FWER·검정력 v2 (tools/run_null_v2.py)

> 합성 시장(실데이터 8심볼 1h 수익률에서 재구성) 한 개 = 한 epoch. 4계열 탐색 → 확인 구간 SR 양수 후보를 SR 순으로 판정 규칙 v2에 제출(질의 k회까지, 불충분은 미차감). FWER = 통과가 1건 이상인 epoch 비율.
> 임시 DB만 썼고 운영 DB에는 쓰지 않았다. 실전 전략의 성과 증거가 아니다.

- 설정: `{"reps": 30, "arm": "random", "alpha": "random", "z": 6.0, "strength": {"bootstrap": 0.002121919923993119}, "z_meaning": "gross hourly z (simple returns) of long-all / fade-last-hour on the synthetic holdout", "sigma_h": 0.010364380007145277, "holdout_days": 700, "k": 20, "budget": {"s1_count": 30, "top_k": 3, "s3_top": 5, "families": ["trend", "mean_reversion", "momentum", "volatility"]}, "rule_hash": "99973210797d0619ab4e93ae7422a4c96c5bc48930271aef4388df22356eb04b", "alpha_per_query": 0.0025, "panel_load_s": 8.2, "wall_s": 2253.1, "peak_private_mb": 850}`

| 귀무 | 반복 | 측정 epoch | 후보 0 | 측정 불가 | FWER | Wilson 95% | 평균 질의 | 평균 후보 | 불충분 비율 |
|---|---|---|---|---|---|---|---|---|---|
| bootstrap | 30 | 28 | 0 | 2 | 0.607 | [0.424, 0.764] | 4.7 | 7.3 | 0.36 |

## 판정 기준(사전 등록, ADR-022·043)
- 귀무(z = 0): FWER의 Wilson 95% 상한이 0.05 이하면 충족.
- 측정 가능 epoch가 0이면 '측정 불가'이지 FWER 0이 아니다.
- 알파(z > 0): 회수율 = 통과 epoch 비율(검정력).
