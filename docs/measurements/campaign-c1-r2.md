# 캠페인 c1 라운드 2 첫 틱 (v2.3 단계 11, 2026-11-22)

> `tools/run_campaign.py --run-id c1 --max-hours 0.6 --polars-threads 2`를 포그라운드 틱 한 번 실행한 기록이다. 예약 실행의 전체 보고서는 저장소 밖 `qr-data/run/campaign-c1.md`에 쓰인다. 측정 기록이며 성과 주장이 아니다.

| 항목 | 값 |
|---|---|
| 재개 | c1 상태(배치 100, 라운드 1, 열린 셀 0)를 그대로 재개했다. 거부되지 않았다 |
| 라운드 전환 | `exhausted`로 끝나지 않고 라운드 2가 열렸다 |
| 재개방 | 펀딩 셀 4개(funding_carry·funding_basis × 4h·1d, top100, w2). c1 때 data_missing이었고 그 뒤 펀딩이 적재됐다. `pull_base` 1로 3회씩 새로 받는다 |
| 추가 | G1의 새 계열 셀 6개(oi_positioning·taker_flow·spot_basis × 4h·1d, top100, w2) |
| 이번 틱 배치 | 2개: taker_flow@4h → no_signal, taker_flow@1d → sparse |
| 종료 | `budget`(시간 예산 0.6시간 소진), exit 0. 다음 틱이 이어서 돈다 |
| 후보 | 0. validator 제출 0(`validator_submitted: []`) |
| 상태 | 셀 42, 은퇴 32, 누적 배치 102 |
| 패널 | 패널 코드가 바뀌어 캐시를 다시 만들었다. w2·4h·top100은 438심볼 920초, w2·1d·top100은 1,156초. 틱 시간 대부분을 여기에 썼다 |

## 발견한 결함과 수정
- **불완전한 데이터로 배치가 돌았다.**
  - metrics 5m을 100심볼 중 약 20개만 받은 상태에서 taker_flow 셀 2개가 실행됐고, 각각 1회를 썼다.
  - 원인: 커버리지가 "창 안에 파일이 하나라도 있으면 사용 가능"으로 판정했다.
- **수정(이 단계).**
  - 다운로드가 끝나지 않은 tier는 그 틱에서 쓰지 않는다. 틱 시작 때 `run/data-queue-state.json`에서 `done`이 아닌 tier를 읽는다.
  - 그 tier의 데이터셋은 계열 패널에서 빼고(셀은 data_missing), 재개방 커버리지에서도 숨긴다.
  - 기본 패널 데이터셋(가격·펀딩 정산)은 빼지 않는다.
  - 상태 파일을 읽을 수 없으면 틱을 거부한다(exit 1).
- **이미 쓴 2회의 처리.**
  - 두 셀은 다음 틱에 data_missing으로 은퇴한다. metrics tier가 `done`이 되면 다시 열리고, 그때 3회를 새로 받는다.
  - 부분 데이터로 돈 trial은 fold(pull 번호)가 달라 재사용되지 않는다.
  - 남는 흔적은 이력의 두 줄(no_signal·sparse)과 bandit의 0 보상 두 번이다.
- 이 두 배치의 결과는 taker_flow 계열에 대한 증거로 쓰지 않는다.
