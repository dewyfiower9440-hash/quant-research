# Phase 0 측정표 (2026-11-08)

- **원시 결과:** `C:\dev\qr-data\measurements\phase0-all-20261108T095346Z.json`
- **실행 명령:** `uv run python tools/phase0_probe.py --ws-seconds 30`. 공개 엔드포인트만 사용했고, API 키는 쓰지 않았습니다. 벌크 데이터는 BTCUSDT 샘플 2개만 받았습니다.
- **추가 측정:** REST bookTicker 1초 폴링 30회. 명령은 이 문서 하단에 있습니다.
- **해석 기준:** 실측값과 추정값(`bytes/day`는 30초 측정치 × 2880)을 구분해 적습니다.

## 1. 네트워크·WebSocket 접속 (30초 창)
| 대상 | 접속 | 메시지/초 | 원본 bytes/일(추정) | 비고 |
|---|---|---|---|---|
| Binance 현물 `stream.binance.com:9443` bookTicker BTCUSDT | 성공 | 36.8 | 0.47GB | |
| Binance 현물 `data-stream.binance.vision` 같은 스트림 | 성공 | 36.8 | 0.47GB | 시세 전용 엔드포인트 |
| **Binance 선물 `fstream.binance.com/public/stream`** bookTicker 상위 20 | **성공** | **1,549.5** | **27.3GB** | 2026-08 기존 보고서의 "fstream 차단"은 **현재 해당 없음** |
| Binance 선물 legacy `fstream.binance.com/stream` 같은 스트림 | 성공 | 1,548 | 27.2GB | legacy 경로도 현재는 동작 |
| Binance 선물 `/market/stream` `!markPrice@arr@1s` | 성공 | 2.0 | 14.4GB | 1초마다 전 심볼 배열이라 큼 |
| Binance 선물 `/market/stream` `!forceOrder@arr` | 성공 | 0.17 | 3.5MB | 작음 |
| Upbit `api.upbit.com/websocket/v1` trade + orderbook(1단계) KRW 상위 20, SIMPLE | 성공 | 29.8 | 0.53GB | |

**CPU:** 위 7개 연결을 asyncio 프로세스 하나에서 30초 동안 받았을 때 CPU 시간은 약 15.2초였습니다. 즉 **코어 약 0.5개를 상시 사용**하며, 대부분 선물 bookTicker(초당 1,550 메시지)의 JSON 처리입니다.

## 2. REST bookTicker 1초 폴링 (선물)
| 항목 | 값 |
|---|---|
| 응답 1회 | **전체 804심볼**, 121,322 bytes |
| 30회 CPU | 0.297초. **코어 약 1% 사용** |
| `x-mbx-used-weight-1m` | 첫 요청 5, 30회 후 40(한도 2400/분 대비 여유) |
| 필드 | `symbol, bidPrice, bidQty, askPrice, askQty, time(이벤트 ms), lastUpdateId` |
| 다운로드 대역폭 | 약 121KB/s, 하루 약 10GB(저장량이 아니라 수신량) |

**결정:** ADR-003 참고.

## 3. 시계 (서버 대비 offset, RTT가 가장 작은 표본)
| 기준 | offset | RTT | 비고 |
|---|---|---|---|
| Binance 현물 `/api/v3/time` | **−16.0ms** | 42ms | 오차 한계는 ±RTT/2 = ±21ms |
| Binance 선물 `/fapi/v1/time` | **−14.5ms** | 43ms | |
| Upbit HTTP `Date` 헤더 | −940ms | 26ms | **해상도가 1초라 offset 기준으로 쓸 수 없음.** 시계 기준은 Binance `/time`으로 합니다 |

## 4. Binance 벌크 샘플 (BTCUSDT, 2026-11-06)
| 데이터셋 | 크기 | 행 수 | 컬럼 | 시각 표기 |
|---|---|---|---|---|
| `futures/um/daily/metrics` | 11,491B zip | 288 | `create_time, symbol, sum_open_interest, sum_open_interest_value, count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, count_long_short_ratio, sum_taker_long_short_vol_ratio` | `create_time` 텍스트(UTC로 가정, **미확인**), 5분 격자 |
| `futures/um/daily/bookDepth` | 560,934B zip | 34,560 | `timestamp, percentage, depth, notional` | 텍스트 초 단위. 첫 행 00:00:01, 마지막 23:59:30, ±1~5% 12단계 × 약 2,880 스냅샷(약 30초 간격, 추정) |

**발견 사항**
- **metrics에 OI, top-trader 비율, 롱숏 비율, taker 비율의 과거 이력이 들어 있습니다**(2020-09~, researcher 조사). 그래서 30일 REST 제한 데이터 중 상당 부분은 **벌크로 과거를 메울 수 있습니다.** 그래도 T0 실시간 수집은 당일 가용성과 관측 lag 측정을 위해 유지합니다.
- **metrics 품질 이상:** 첫 세 행이 00:00, 00:05, **00:20**입니다(00:10과 00:15가 빠짐). 행이 288개인데 마지막 행이 23:50이라 중간 결측이나 중복이 있을 수 있습니다. 정규화 단계에서 격자 대조와 gap 등록이 필요합니다.
- **시각이 구간 시작인지 끝인지는 미확인입니다.** 공식 문서에 정의가 없습니다. 설계 v2.1 §5.4 규칙에 따라 **구간 시작으로 보수적으로 가정**합니다(가용 시각 = `create_time` + 5분 + lag).

## 5. Upbit
| 항목 | 값 | 방법 |
|---|---|---|
| KRW-BTC 1분봉 최대 과거 | **2017-09-25**(상장 초기까지 조회됨) | 날짜 이진 탐색 12회(단조성 가정) |
| `ask_bid` 의미 | 60초 동안 41건 체결: `BID`가 최우선 매도호가 이상에서 37건, 호가 사이에서 4건. **BID = 매수 주도로 판단(부분 확인).** 이 창에서 `ASK` 체결이 없어 반대 방향은 **미확인**입니다 | 직전 최우선 호가와 비교 |

## 6. 연산 마이크로벤치마크 (5백만 행, 합성 데이터)
| 작업 | 시간 |
|---|---|
| Polars group_by mean+std(100그룹) | 0.185s |
| Polars rolling_mean(60) over 100심볼 | 0.601s |
| DuckDB group_by mean+std | 0.100s |
| Numba 경로 의존 루프(첫 호출, JIT 포함) | 3.247s |
| Numba 같은 루프(재호출) | 0.147s |

## 7. 호스트
| 항목 | 값 |
|---|---|
| RAM | 전체 15.57GB, **측정 시 여유 4.03GB** |
| 디스크 C: 여유 | 322.9GB |
| SSD SMART 온도 | **63°C**(한계 100°C, Wear 0). 2026-11-08 23:19 KST, 관리자 권한 읽기 전용 조회 1회(ADR-009) |

## 8. T0 저장량 재추정 (실측 기반)
- **선물 bookTicker:** REST 1초 샘플로 상위 50심볼을 저장하면 하루 432만 행입니다. 압축 Parquet으로 수십~100MB 수준(추정). 원본 JSONL은 응답 전체를 저장하지 않고, 선택한 심볼의 행만 저장합니다.
- **markPrice:** 원본 `@arr`는 하루 14GB라서 **T0에서 저장하지 않습니다.** 과거 mark price는 벌크 `markPriceKlines`(1분)로 대체합니다. 모의매매(Phase 6)에서는 필요한 심볼만 구독합니다.
- **Upbit trade + 호가 1단계:** 하루 원본 약 0.5GB(추정)입니다. 호가는 1초 샘플로 줄입니다.
- **실제 저장량은 작업 3에서 1시간 실측해 갱신합니다.** 이번 측정은 30초 창을 외삽한 값입니다.

## 추가 측정 명령 (REST bookTicker)
`uv run python -c "...httpx GET https://fapi.binance.com/fapi/v1/ticker/bookTicker 30회, 1초 간격, process_time과 응답 크기, weight 헤더 기록..."` (2026-11-08 18:5x KST, 결과는 위 2절)
