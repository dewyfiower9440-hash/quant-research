---
status: blocked
blocked_reason: step 3 needs the user to register the Upbit backfill job; parked so the stale approval cannot re-arm (2026-11-11)
task: qr-phase1-completion
---

# Phase 1 마무리 (2026-11-09 사용자 지시 "현재 상태 확인 후 phase0~1 완료하자")

실행 계획 `2026-11-08-phase0-1-build.md`의 미완료 단계(4, 8, 9, 10, 11)를 끝냅니다. Phase 2 이후, 실거래, API 키는 범위 밖입니다.

## 시작 시점 상태 (2026-11-09 13:06Z 점검)
- **벌크 klines:** 345/632 심볼, 가중 57.7%, ETA 약 6.3시간, 실패 3건
  - 실패 3건은 MATICUSDT premium 2021-06~08입니다. 절전에서 깨어난 직후 DNS 실패로 생겼고, 다음 실행 때 재시도됩니다.
- **수집기:** 08:27Z부터 재시작 없이 가동 중입니다.
  - rest 58MB, ws 24MB
  - basis 400 오류가 23개 심볼에서 반복됩니다(TradFi 계약).
- **두 번째 절전:** 11:39:39Z~13:02:55Z(83분). 수신 공백 스캔이 다음 주기에 기록할 예정입니다.
- **Upbit 백필 작업, SSD 감시 작업:** 등록되지 않았습니다.

## 설계 결정
- **카탈로그 백업(4단계)**
  - 백업 job이 `VACUUM INTO`로 `meta/catalog.db` 스냅샷을 백업 폴더에 씁니다.
  - 스냅샷의 sha256과 테이블별 행 수를 manifest에 기록합니다.
  - 복원 리허설은 원본 파일과 스냅샷의 sha256·줄 수, 그리고 스냅샷 DB의 테이블별 행 수를 비교합니다.
- **모니터(10단계)**
  - rest 작업 프로세스에 5분 주기 job을 둡니다.
  - 점검 판정은 순수 함수 `monitor/checks.py`가 맡습니다.
    - 점검 항목: ws 하트비트 지연, 디스크 여유, 가용 RAM, 시계 offset과 측정 지연, SSD 상태 파일, 새 수신 공백(5분 이상)
  - 판정 결과는 catalog `incident` 테이블(마이그레이션 0006)에 열림·닫힘으로 기록합니다. 같은 키가 열려 있으면 중복으로 열지 않습니다.
  - 새로 열린 incident만 `run/alerts.log`에 남기고, 로그온 세션에 토스트 알림(`tools/notify.ps1`)을 띄웁니다.
    - 토스트는 hidden PowerShell로 띄우고, 메시지는 `run/alerts.json`에서 읽습니다.
  - basis 400(-4104) 심볼은 지원하지 않는 심볼로 보고 해당 프로세스 수명 동안 건너뜁니다. 경보 소음을 없애기 위해서입니다.
  - rest 프로세스 자신이 멈춘 경우(hang)는 이 모니터가 잡지 못합니다. supervisor가 죽은 프로세스를 재시작하는 범위까지만 보장하며, 이 한계를 문서에 기록합니다.
- **T1 추가 데이터(8단계)**
  - `tools/bulk_extras.py`가 아래 데이터를 순서대로 받습니다.

    | 데이터 | 대상 심볼 | 기간 | 저장 방식 |
    |---|---|---|---|
    | funding(월 파일) | 전체 유니버스 | 소속 월 | 파일 그대로 |
    | bookDepth(일 파일) | 소속 월 수 상위 20 | 2023-01 이후 | 월 단위로 합쳐 Parquet 1개 |
    | metrics(일 파일) | 전체 유니버스 | 소속 월 | 월 단위로 합쳐 Parquet 1개 |
    | 현물 klines 1m(월 파일) | 현물 쌍이 있는 유니버스 심볼 | 소속 월 | 파일 그대로 |

  - 끝난 달만 받습니다. 일 파일은 모두 `bulk_file`에 기록하고, 한 달 안에 새로 생겼거나 재생성된 파일이 있으면 그 달을 새 버전으로 다시 씁니다.
  - metrics는 일 파일 안의 중복 행을 제거하고, 5분 격자에서 빠진 칸을 gap으로 기록합니다.
  - 시각은 Phase 0 규칙을 따릅니다.
    - metrics: `unknown_interval`(5분)
    - funding, bookDepth: 이벤트로 보고 기본 lag 1분
  - 벌크 klines와 같은 `run/bulk.lock`을 써서 동시에 돌지 않게 합니다. 작업 이름은 `qr-bulk-extras`입니다.
  - 2024-03-30 이전 bookTicker 벌크는 스프레드 추정량 보정용으로만 쓰므로 가설이 요구할 때(T2) 받습니다. 이 결정은 ADR로 남깁니다.
- **벌크와 REST 대조(8단계 완료 조건):** `tools/validate_bulk_klines.py`
  - 다운로드된 심볼·월에서 무작위 표본을 뽑습니다.
  - REST(`/fapi/v1/klines`, `markPriceKlines`, `indexPriceKlines`, `premiumIndexKlines`) 1500분 구간과 OHLCV를 비교합니다.
- **7일 검증(11단계):** `tools/validate_week.py`
  - 실시간 소스별 커버리지(1 − gap 시간 / 관측 기간)
  - OI·비율의 재수집 수정률
  - 강제청산과 호가 공백의 일치 여부
  - 보고서는 `docs/measurements/validate-week.md`에 씁니다.
  - 7일이 지나야 최종 판정이 납니다(2026-11-15 이후). 지금은 중간 보고만 합니다.

## 단계
- [x] 1. 카탈로그 스냅샷 백업과 복원 리허설(테스트 먼저). 실데이터로 리허설하고 4단계 완료 조건을 확인합니다.
- [x] 2. 모니터: 마이그레이션 0006, `monitor/checks.py`, rest job, 알림, basis 소음 제거(테스트 먼저). 재시작해 적용하고, 강제 종료 후 복구를 실측합니다.
- [ ] 3. Upbit 분봉 전체 백필 작업 등록(step9 계획 5단계)과 진행 보고 — **BLOCKED(사용자 실행 필요)**: 자동 모드 분류기가 작업 등록을 거부했습니다(2026-11-09, 2회).
- [x] 4. T1 추가 데이터: 파서, 월 묶음 다운로더, 실행 도구, 작업 등록(테스트 먼저). 소규모 파일럿을 먼저 돌립니다. (작업 등록 스크립트는 작성했고, 등록 실행은 3단계와 같은 이유로 사용자가 합니다.)
- [x] 4b. 카탈로그 `universe_membership`(유니버스 보고서의 시점별 top-100 구간)과 `instrument`(Binance exchangeInfo, Upbit 마켓 스냅샷)을 채웁니다. 1단계 리허설에서 두 테이블이 0행인 것을 발견했고, 설계 §5.3·§5.4가 요구하는 항목입니다.
- [x] 5. 벌크와 REST 표본 대조 도구와 실행 결과
- [x] 6. 7일 검증 도구와 중간 보고
- [x] 7. 문서(progress, decisions, failures, operations, test-results, 실행 계획 체크박스)

## 사용자 결정·외부 조건으로 남는 것
- **재부팅 후 5분 안 복구 확인:** 사용자 승인이 필요합니다.
- **SSD 감시 작업 등록:** UAC 승인이 필요합니다.
- **외부 백업 대상:** 정해지지 않았습니다.
- **절전 설정:** OS 설정이므로 바꾸지 않습니다.
- **7일 검증 최종 판정:** 2026-11-15 이후에 납니다.

## 검증
- 단계마다 선언 게이트를 통과하고, 커밋·push 후 CI를 확인합니다.
- 실데이터로 동작을 확인한 결과는 progress에 기록합니다.
