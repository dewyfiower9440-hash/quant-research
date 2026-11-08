# 장애·실패 이력

형식: 번호, 날짜, 작업 번호, 증상(원문 오류 메시지), 재현 방법, 원인 가설, 분석, 수정, 재검증, 시도 횟수, 상태(해결/BLOCKED).
같은 방식이 두 번 실패하면 BLOCKED로 표시하고, 서브에이전트로 독립 분석한 결과를 함께 적습니다.

## F-001 (2026-11-08, 작업 1) 작업 폴더 삭제 명령 차단
- **증상:** `rmdir /c/dev/quant-research`가 Claude Code 내장 안전 검사에서 거부됐습니다(작업 디렉터리 삭제로 분류).
- **원인:** 안전 검사가 작업 공간 디렉터리 삭제를 사람의 승인 없이 막습니다. 의도된 동작입니다.
- **분석:** 삭제는 필요하지 않았습니다. scaffold의 검사 조건은 `root.exists() and any(root.iterdir())`라서 빈 폴더를 허용합니다(`C:\dev\tools\scaffold\new_project.py:591`).
- **수정:** `docs/`만 옮기고 빈 폴더에 생성했습니다. 우회 시도는 하지 않았습니다.
- **재검증:** scaffold 정상 생성, `docs/` 복원 확인
- **시도 횟수:** 1
- **상태:** 해결

## F-002 (2026-11-08, 작업 2) Phase 0 측정 스크립트 Polars 집계 오류
- **증상:** `polars.exceptions.DuplicateError: column with name 'px' has more than one occurrence`. 발생 위치는 `tools/phase0_probe.py`의 `probe_compute`입니다.
- **재현:** `uv run python tools/phase0_probe.py --ws-seconds 30`
- **원인:** `agg(pl.col("px").mean(), pl.col("px").std())`의 두 결과 컬럼 이름이 모두 `px`라서 중복됐습니다. 제 코드 실수입니다.
- **수정:** 각각 `.alias("px_mean")`, `.alias("px_std")`를 붙였습니다.
- **재검증:** 같은 명령을 다시 실행합니다(결과는 `test-results.md`).
- **교훈:** Polars에서 같은 컬럼에 여러 집계를 걸 때는 alias가 필수입니다.

## F-003 (2026-11-08, 작업 3) 테스트 작성 오류: fsync 횟수 기대값
- **증상:** `test_segment_fsyncs_every_n_lines_and_on_close`가 `assert (2 - 0) == 1`로, `..._when_time_interval_elapses`도 비슷하게 실패했습니다.
- **원인:** 테스트 쪽 실수입니다. 세그먼트는 첫 쓰기 때 열리고(lazy open), 그때 헤더를 쓰면서 fsync를 1회 합니다. 그런데 테스트는 이 호출을 기준선(base)에 넣어 빼려고 했고, 열기 전에 기준선을 쟀기 때문에 셈이 틀렸습니다. 구현은 설계대로 동작했습니다.
- **수정:** 기준을 완화하지 않았습니다. 대신 헤더, N줄, 시간 창, close 각각의 fsync를 **총 호출 횟수로 정확히** 확인하도록 고쳤습니다.
- **재검증:** 10 passed
- **상태:** 해결

## F-004 (2026-11-08, 작업 3) ws 프로세스 heartbeat가 메시지가 많을 때 기록되지 않음
- **증상:** 1시간 실측을 시작하고 30초 뒤 `C:\dev\qr-data\run\`에 `rest.heartbeat.json`은 있었지만 `ws.heartbeat.json`은 없었습니다.
- **원인:** `ws_main`이 `health.write()`를 수신 타임아웃(10초 동안 메시지 없음)일 때만 호출했습니다. 청산 메시지가 계속 들어오면 heartbeat가 영원히 기록되지 않습니다. 이대로면 모니터(작업 10)가 정상 프로세스를 죽은 것으로 판단합니다.
- **수정:** 마지막 기록 후 `heartbeat_s`(10초)가 지나면 메시지가 계속 오더라도 heartbeat를 씁니다. 회귀 테스트 `test_ws_main_writes_heartbeat_while_messages_keep_arriving`(수정 전에는 실패 확인)를 추가했습니다.
- **재검증:** 게이트 통과, CI run 42471562536 성공(커밋 19e067c)
- **상태:** 해결

## F-005 (2026-11-08, 작업 3) 독립 코드 리뷰에서 발견된 원본 캡처 결함 11건
- **출처:** `code-reviewer` 서브에이전트(커밋 aaba1ac 대상). 메인 세션이 다음을 직접 재확인했습니다.
  - 1번: 실데이터 `binance.um.force_order` 첫 레코드가 `"offset_ms":null`
  - 3번: `time.sleep(min(delay, poll_s))` 코드
  - 5번: `_open`이 `_last_sync_ns = t`로 설정된 뒤 첫 레코드가 시간 기준 fsync 대상에서 빠지는 논리
- **결함과 수정**

| # | 심각도 | 결함 | 수정 | 회귀 테스트 |
|---|---|---|---|---|
| 1 | high | ws 프로세스가 시계를 측정하지 않아 청산 데이터 offset이 전부 null | ws가 자체 `measure_clock`을 60초 주기로 실행 | `test_ws_main_measures_its_own_clock_reconnects_and_records_the_gap` |
| 2 | high | Upbit 418이나 451이 rest 프로세스 전체를 영구 정지(Binance T0까지 멈춤). Binance 418은 rest만 멈추고 ws는 계속 동작. WS 핸드셰이크 418은 무한 재연결 | `classify_status`를 거래소별로 처리: Upbit 418은 재시도, 451은 해당 작업만 실패. Binance 418은 exit 3 → supervisor가 **전체 정지**. WS 핸드셰이크 418도 ban으로 처리 | `test_classify_status_upbit_418_...`, `test_ws_main_handshake_418_is_a_ban`, `test_supervisor_stops_everything_...` |
| 3 | high | supervisor 재시작 backoff가 `min(delay, poll_s)`라 사실상 무력(약 10초마다 재시작) | 재시작 예정 시각 방식(non-blocking) | `test_supervisor_restarts_crashing_child_with_backoff_...`(16초 동안 정확히 1회) |
| 4 | medium | gap이 "소스별 첫 쓰기"에 등록되어, 드문 소스는 최대 24시간 넘는 가짜 gap이 생김. 공유 소스(incidents)에서 상대 프로세스 파일을 torn tail로 오인 | gap을 **프로세스 단위**로 등록(이전 heartbeat부터). incidents를 프로세스별로 분리(`collector.incidents.rest/ws`). ws 재연결과 23시간 갱신 구간도 gap으로 기록 | `test_rest_main_..._records_its_restart_gap`, ws 재연결 gap 테스트 |
| 5 | medium | 드문 소스의 마지막 줄이 다음 쓰기(수 시간 뒤)까지 fsync되지 않음 | `sync_if_due` + 2초 주기 fsync 작업 | `test_sparse_source_is_synced_by_the_timer_without_another_write` |
| 6 | medium | 쓰기가 실패한 뒤에도 같은 파일에 이어 써서, 이후 유효 레코드까지 무효가 됨 | 쓰기 오류가 나면 파일을 포기하고 새 파일로 이동. quarantine에 `valid_bytes` 기록 | `test_write_failure_abandons_the_file_...`, `test_quarantine_tail_...` |
| 7 | medium | 429 대기를 응답받은 코루틴만 지키고 다른 폴러는 계속 요청 → 418 위험 | 거래소(IP)별 공유 `VenueGate` | `test_rate_limit_wait_seen_by_one_fetcher_blocks_the_other_...` |
| 8 | medium | 장기 작업(metrics 약 5,400회, 첫 Upbit 페이징)이 stop을 무시. 한 심볼 오류로 나머지 전체를 6시간 건너뜀. `now`가 실행 도중 오래된 값이 됨 | 페이지마다 stop 확인, 심볼 단위로 예외 격리(`on_error` → incident), 심볼마다 now 재계산 | `test_metrics_refetch_isolates_...`, `test_metrics_refetch_stops_promptly_and_bans_propagate` |
| 9 | low | cursor가 진행하지 않으면 무한 루프 가능. 7일 넘게 중단되면 누락이 조용히 지나감 | cursor 비진행 시 오류. 7일 창을 넘은 구간은 gap 기록. 중단된 패스는 cursor를 저장하지 않음 | `test_upbit_cursor_that_does_not_advance_...`, `..._interrupted_pass_keeps_the_old_cursor`, `..._outage_longer_than_the_api_window_...` |
| 10 | low | suspect일 때 offset 값 자체를 버림 | offset은 항상 기록하고 `clock_suspect` 플래그를 별도로 둠(세그먼트 형식 v2) | `test_offset_is_kept_even_when_the_clock_is_suspect` |
| 11 | low | supervisor 종료 시 자식을 강제 종료해 최종 fsync와 heartbeat가 생략됨 | STOP 파일로 정상 종료를 요청하고 grace 시간 뒤에만 terminate | `test_supervisor_stops_everything_...`(STOP 파일로 정상 종료 확인), `test_rest_main_stops_when_the_stop_file_appears` |

- **수정 중 생긴 테스트 오류 1건:** supervisor 테스트에서 첫 backoff를 5초로 잘못 계산했습니다(실제는 10초). 실행 시간을 16초로 늘리고 "정확히 1회 재시작"으로 **더 엄격하게** 고쳤습니다.
- **재검증:** 선언 게이트 통과(97 passed, 커버리지 91.49%, pyright 0 errors). CI는 커밋 후 기록합니다.
- **영향받은 데이터:** 수정 전 코드로 수집한 첫 1시간 실측분(헤더 git SHA aaba1ac)은 다음 상태입니다. 원본은 보존하며, 정규화 단계에서 SHA로 구분합니다.
  - force_order의 `offset_ms`가 null입니다. 같은 시간대 `binance.server_time` 기록으로 복원할 수 있습니다.
  - gap 기록은 v1 의미(소스별)로 남아 있습니다.
- **상태:** 해결(CI 확인 후 확정)
- **시도 횟수:** 1
- **상태:** 해결(재실행 결과 확인 후 확정)

## F-006 (2026-11-08, 작업 4b) 작업 스케줄러가 크래시 후 재시작하지 않음
- **증상:** 22:41:53에 수집 프로세스 트리를 강제 종료했는데, 7분 동안 재시작되지 않았습니다.
  - `Get-ScheduledTaskInfo`: `LastTaskResult 4294967295`, `NextRunTime` 비어 있음
- **재현:** 로그온 트리거에 5분 반복(`Repetition PT5M`)을 붙여 등록 → `Start-ScheduledTask` → 프로세스 강제 종료
- **원인:** 로그온 트리거에 붙인 반복은 **로그온 이벤트가 실제로 발생한 뒤에만** 활성화됩니다. 이미 로그온한 상태에서 등록하면 반복이 시작되지 않습니다(`NextRunTime`이 비어 있음). `RestartCount`도 강제 종료된 경우에는 재시작하지 않았습니다.
- **수정:** 트리거를 두 개로 둡니다(`tools/register_collector_task.ps1`).
  - (1) 로그온 트리거: 로그인 직후 즉시 시작
  - (2) 시간 트리거: 등록 1분 뒤부터 5분마다 무기한 반복
  - `MultipleInstances IgnoreNew`와 인스턴스 잠금으로 중복 실행을 막습니다.
- **재검증**
  - 22:51:42에 강제 종료 → **22:56:10 자동 재시작**(268초)
  - restart gap 기록: 22:51:40~22:56:10, 271초
  - 중복 실행 시도 → exit 2(supervisor, rest 모두)
- **영향:** 22:41:50~22:49:18(449초) 수집 공백이 생겼고 gap으로 기록했습니다. 그 사이 수동으로 재가동했습니다.
- **상태:** 해결. **재부팅 후 재개는 미확인**(사용자가 재부팅할 때 확인)
