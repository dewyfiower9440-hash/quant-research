# 캠페인 c1 재집계: 라벨 v3 (ADR-050)

> 저장된 배치 요약에 현재 라벨 함수만 다시 적용했다. 평가를 다시 하지 않았고 trial은 늘지 않았다.
> 이 표는 측정이고 성과 주장이 아니다.

- 배치 수: 103
- S3 판정 수: 78 (확인 SR 양수 18)

## 기록된 원인별 라벨 v3

| 기록된 원인 | below_bar | cost_dominated | data_missing | no_signal | sparse | unstable | 합계 |
|---|---|---|---|---|---|---|---|
| cost_dominated | 0 | 2 | 0 | 0 | 0 | 0 | 2 |
| data_missing | 0 | 0 | 5 | 0 | 0 | 0 | 5 |
| no_signal | 0 | 0 | 0 | 18 | 0 | 0 | 18 |
| sparse | 0 | 0 | 0 | 0 | 19 | 0 | 19 |
| unstable | 14 | 0 | 0 | 0 | 0 | 45 | 59 |
| 합계 | 14 | 2 | 5 | 18 | 19 | 45 | 103 |

## S3 판정이 있었던 배치 (기록된 원인별)

| 기록된 원인 | S3 판정 | 확인 SR 양수 |
|---|---|---|
| cost_dominated | 2 | 0 |
| data_missing | 0 | 0 |
| no_signal | 17 | 4 |
| sparse | 0 | 0 |
| unstable | 59 | 14 |

## `below_bar` 하위 사유

- no_cross: 14

## `sparse` 배치와 lookback 상한 (ADR-051)

> 신호 창 매개변수만 본다. 비중의 변동성 창과 평활은 세지 않으므로 실제 초과 비율은 이보다 높을 수 있다.

| 셀 | sparse 배치의 S2 점 | 상한 초과 | 비율 |
|---|---|---|---|
| breakout@1d|top100|w2 | 12 | 5 | 0.42 |
| breakout@4h|top100|w2 | 21 | 2 | 0.10 |
| liquidity@1d|top100|w2 | 13 | 5 | 0.38 |
| liquidity@4h|top100|w1 | 9 | 4 | 0.44 |
| low_vol@1d|top100|w1 | 16 | 10 | 0.62 |
| low_vol@1d|top100|w2 | 8 | 8 | 1.00 |
| low_vol@4h|top100|w1 | 7 | 3 | 0.43 |
| mean_reversion@1d|top100|w1 | 9 | 7 | 0.78 |
| mean_reversion@4h|top100|w2 | 12 | 0 | 0.00 |
| taker_flow@1d|top100|w2 | 6 | 1 | 0.17 |
| xs_reversal@1d|top100|w1 | 5 | 0 | 0.00 |
| xs_reversal@4h|top100|w2 | 5 | 0 | 0.00 |
| 합계 | 123 | 45 | 0.37 |
