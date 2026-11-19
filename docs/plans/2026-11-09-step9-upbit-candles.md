---
status: blocked
blocked_reason: step 5 needs the user to register the full backfill job; parked so the stale approval cannot re-arm (2026-11-11)
task: qr-phase0-1-step9
---

# 작업 9: Upbit 1분봉 백필 (Phase 0~1 계획 작업 9의 세부 설계)

## API 실측 (2026-11-09, 공개 GET 몇 건)
- `GET /v1/candles/minutes/1?market&to&count<=200`
  - `to`는 배타적입니다. 결과는 최신 봉부터 반환됩니다.
  - **`to` 이전에 실제로 존재하는 봉을 최대 200개까지** 반환합니다. 거래가 드문 KRW-USDG는 200개가 10일에 걸쳐 있었습니다.
  - 상장 이전 시점을 요청하면 `[]`를 반환합니다. KRW-BTC는 2017-09-25 03:00 UTC가 첫 봉입니다.
- 필드
  - `candle_date_time_utc`: 구간 시작 시각
  - `timestamp`: 구간 마지막 체결 시각
  - OHLC, `candle_acc_trade_price`, `candle_acc_trade_volume`
- 레이트 리밋: 헤더 `Remaining-Req: group=candles; min=600; sec=10`. 실시간 수집기는 candles 그룹을 쓰지 않습니다.

## 설계
- **페이지 처리**
  - 고정된 끝 시각(`end`, 첫 실행 때 정해 manifest에 기록)에서 과거 방향으로 페이지를 넘깁니다.
  - 다음 `to`는 직전 페이지에서 가장 오래된 봉의 시각입니다.
  - 빈 응답을 받으면 해당 마켓은 완료입니다.
- **no_trade**
  - 연속으로 받은 봉 사이의 빈 분은 "체결 없음"으로 확정됩니다(API가 존재하는 봉만 연속으로 주므로).
  - 이 구간은 gap(cause `no_trade`)으로 병합해 기록합니다. 상장 이전 구간은 gap이 아닙니다.
  - 기록량은 파일럿에서 실측한 뒤 조정합니다.
- **저장**
  - 페이지 N개(기본 50개, 최대 1만 봉)마다 불변 Parquet 하나를 씁니다.
  - catalog `backfill_chunk`(마이그레이션 0004)와 no_trade gap을 **하나의 트랜잭션**으로 기록합니다.
  - 재개는 마켓별로 가장 오래된 청크의 `start`에서 이어 갑니다. 완료는 `backfill_done`에 기록합니다.
- **데이터셋:** `upbit.candles_1m`
  - 시각 의미: `interval_start`, 간격 60초
  - lag 사전값: 60초
  - 봉인: 벌크와 같은 경계(연구 구간 < 2026-07-01, holdout 2026-07-01~2026-11-01, ADR-013과 일관)
- **유니버스:** 현재 KRW 24시간 거래대금 상위 20(`upbit_top_n`)
  - **생존 편향 제한:** 상장폐지 마켓의 캔들 조회 가능 여부는 미확인입니다. 목록과 선정 시각은 manifest에 기록합니다.
- **실행:** 대량 다운로드와 같은 패턴으로 진행합니다.
  - 작업 스케줄러에서 일반 사용자 권한, 우선순위 7로 실행합니다.
  - 단일 인스턴스 잠금과 자원·온도 확인을 적용합니다.
  - 초당 6회 이하로 요청하고, `Remaining-Req`에 따라 속도를 맞춥니다. Upbit 418이 오면 즉시 중지합니다(IP 차단은 실시간 수집에도 영향을 주므로).
- **검증(완료 조건)**
  - fixture 테스트(페이지 처리, no_trade, 재개, 원자성, 완료 처리)
  - **샘플 구간 대조:** T0로 수집한 `upbit.trades`를 1분 OHLCV로 집계해, 같은 구간의 백필 봉과 대조합니다.

## 단계
- [x] 1. 마이그레이션 0004, bootstrap에 데이터셋 등록, 순수 파서와 no_trade 계산(테스트 먼저)
- [x] 2. 비동기 백필 루프(Fetcher 재사용)와 재개·완료 처리(MockTransport 테스트)
- [x] 3. 실행 도구, 메모리 측정 어댑터를 공유 모듈로 이동, 작업 등록 스크립트
- [x] 4. 파일럿(KRW-BTC 소량) 실행, trades 대조, no_trade 기록량 실측
- [ ] 5. 전체 백필 작업 등록, 진행 보고, 문서화
