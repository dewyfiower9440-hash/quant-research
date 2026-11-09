# 7일 연속 운영 검증 (plan step 11)

- 생성: 2026-11-09 14:13Z (`tools/validate_week.py`)
- 창: 2026-11-08 10:21Z ~ 2026-11-09 14:13Z = **1.16일**. **중간 보고(7일 미만, 2026-11-15 이후 최종)**

## 1. 실시간 소스 커버리지 (기록된 gap 기준)
| 소스 | 커버리지 | 결측 시간(분) | 결측 구간 수 | 가장 긴 구간 |
|---|---|---|---|---|
| `binance.um.book_ticker_1s` | 82.2818% | 296.1 | 15 | 2026-11-09 03:36Z 190.2분 |
| `upbit.orderbook_l1_1s` | 68.9349% | 519.2 | 16 | 2026-11-09 03:36Z 190.2분 |
| `binance.um.force_order` | 82.3075% | 295.7 | 12 | 2026-11-09 03:36Z 190.3분 |

- 양쪽 1초 소스에 함께 있는 공백(호스트 전체, 절전 등): 10건, 292.7분. 강제청산 WS 결측에 포함했습니다.

## 2. gap 원인별 (창 안, 벌크·백필 제외)
| source | cause | detail | 건수 | 합계(분) |
|---|---|---|---|---|
| `binance.um.book_ticker_1s` | unknown | receive_gap | 13 | 294.8 |
| `binance.um.force_order` | reconnect | - | 2 | 1.3 |
| `process.rest` | restart | - | 12 | 19.2 |
| `process.ws` | restart | - | 10 | 20.1 |
| `upbit.orderbook_l1_1s` | unknown | receive_gap | 13 | 517.9 |

## 3. OI·비율 30일 재수집 대조 (바이템포럴 버전)
| 데이터셋 | 행(버전 포함) | 키 | 값이 바뀐 키 | 수정률 |
|---|---|---|---|---|
| `binance.um.oi_hist` | 621,135 | 621,135 | 0 | 0.000% |
| `binance.um.top_ls_account` | 621,062 | 621,062 | 0 | 0.000% |
| `binance.um.top_ls_position` | 621,049 | 621,049 | 0 | 0.000% |
| `binance.um.global_ls_account` | 613,038 | 613,038 | 0 | 0.000% |
| `binance.um.taker_ls` | 604,375 | 604,375 | 0 | 0.000% |
| `binance.um.basis` | 391,459 | 391,459 | 0 | 0.000% |

## 4. 벌크 1분봉 vs REST (`tools/validate_bulk_klines.py`)
- rest-compare-20261109T140214Z.json: 표본 38/40 일치, 비교 봉 19,000 (seed 20261109)
- rest-compare-20261109T140416Z.json: 표본 40/40 일치, 비교 봉 20,000 (seed 20261109)
- rest-compare-20261109T140625Z.json: 표본 60/60 일치, 비교 봉 29,956 (seed 7)

## 5. 모니터 incident
| key | 심각도 | 열림 | 닫힘 |
|---|---|---|---|
| `ssd_monitor` | warn | 2026-11-09 13:38Z | - |
| `gap:binance.um.book_ticker_1s:1794224377106018100` | warn | 2026-11-09 14:03Z | 2026-11-09 14:08Z |
| `gap:upbit.orderbook_l1_1s:1794224378718615800` | warn | 2026-11-09 14:03Z | 2026-11-09 14:08Z |
