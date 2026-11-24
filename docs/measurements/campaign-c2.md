# 캠페인 c2 (tools/run_campaign.py)

> 연구 역할, 봉인 epoch 2 연구 창 안. 측정 기록이며 성과 주장이 아니다. 후보는 validator 판정 전이다.

| 항목 | 값 |
|---|---|
| run_id | "c2" |
| batches | 2 |
| cells_tried | 1 |
| causes | {"no_signal": 1, "below_bar": 1} |
| candidates | [] |
| best_sr_net_by_cell | {"liquidity@1d\|top100\|w2": 0.020093227680498496} |
| next_data | ["binance.um.bulk.funding", "binance.um.bulk.metrics_5m", "binance.spot.bulk.klines_1m", "binance.um.bulk.book_depth", "binance.um.bulk.agg_flow_1m", "external (alternative.fng_1d, coinmetrics.asset_1d, deribit.dvol_1h)"] |
| status | "budget" |
| reason | "time budget used" |
| ran | 2 |

## 다음 배치의 결합 기준과 검정력 (보고용, 기준선은 바꾸지 않는다)

- 누적 N(두 확인 구간, 다음 배치의 2건 포함): 83
- 결합 확인 구간: 456일, 기준: 연 SR 2.20

| 참 연 SR | 통과 확률(iid 근사) |
|---|---|
| 1 | 0.09 |
| 1.5 | 0.22 |
| 2 | 0.41 |
| 2.5 | 0.63 |
| 3 | 0.81 |

## 배치

> best S2의 손익 분해(자본 대비): 순수익 = 가격 손익 - 비용 + 펀딩. 펀딩이 음수면 지급이다.

| # | 셀 | 원인 | 보상 | best S2 SR | 순수익 | 비용 | 펀딩 | S3 SR | 교차 S3 SR | 결합 SR | 결합 기준 | S3 trial | 오류 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | liquidity@1d\|top100\|w2 | no_signal | 0.16879542294252575 | -0.014358111332465987 | -0.013761708010217255 | 0.003161236203331735 | 0.0029724308097373343 | -0.06878372954526217 | None | None | 0.0 | 34185 |  |
| 2 | liquidity@1d\|top100\|w2 | below_bar | 0.09718306815871888 | 0.020093227680498496 | 0.01961087013037277 | 0.0009372770599078434 | -0.0028896868069820327 | 0.06049125203456374 | 0.09671479117656179 | 0.08483808119175543 | 0.11524484406755726 | 34312 |  |
