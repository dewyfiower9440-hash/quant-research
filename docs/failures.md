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

## F-007 (2026-11-09, 작업 6·7) `.gitignore`의 `data/`가 `src/quant_research/data/`와 `tests/data/`를 무시함
- **증상:** CI run 42534955542가 실패했습니다. pyright 메시지: `Import "quant_research.data.compact" could not be resolved`. 로컬 게이트는 통과했습니다.
- **원인:** scaffold가 만든 `.gitignore` 19행의 `data/`는 저장소 루트의 데이터 폴더를 막으려던 것입니다. 그런데 슬래시 없는 패턴이라 **모든 경로의 `data/`**에 걸렸고, `src/quant_research/data/`(access, lake, compact)와 `tests/data/`가 커밋되지 않았습니다(`git ls-files` 0건).
- **영향**
  - **작업 6의 CI 성공(run 42533347966)은 해당 코드와 테스트를 포함하지 않았습니다.** 작업 6의 "DONE" 판정은 잘못이었고, 이번 수정 커밋의 CI로 다시 확정합니다.
  - 로컬 게이트는 작업 트리 기준이라 통과했습니다. 반면 CI는 저장소 기준이라 이 차이가 드러났습니다.
- **수정:** `data/`를 `/data/`(루트 전용)로 바꾸고, 누락된 파일 9개를 커밋합니다.
- **재발 방지**
  - 프로젝트 Gotcha를 추가합니다.
  - dev-tools scaffold 템플릿의 같은 패턴은 공유 도구라 범위 밖입니다. 사용자에게 보고하고 수정을 제안합니다.
  - 다음부터 단계를 완료할 때 `git status --porcelain --ignored`로 무시된 소스 파일이 있는지 확인합니다.
- **재검증:** 수정 커밋의 CI run(아래 test-results)
- **상태:** 해결(CI run 42536249442, 169 passed)

## F-008 (2026-11-09, 작업 8) 벌크 CSV 타입 추론 실패로 전체 다운로드 중단
- **증상:** 전체 다운로드가 14초 만에 중단됐습니다(파일 25개 처리 후). 오류: `polars.exceptions.ComputeError: could not parse '0.00000000' as dtype 'i64' at column 'quote_volume'`
- **원인**
  - (1) Polars가 앞쪽 100행만 보고 열 타입을 추론합니다. mark·index 파일은 `quote_volume` 열이 앞쪽에서 `0`이라 정수로 추론됐고, 뒤에서 `0.00000000`이 나와 실패했습니다.
  - (2) `PolarsError`가 파일 단위 예외 처리(`httpx.HTTPError`, `ValueError`, `BadZipFile`) 범위 밖이라, 파일 하나의 실패가 실행 전체를 멈췄습니다.
- **재현 과정**
  - 처음 쓴 테스트는 4행짜리라 추론 범위 안에 실수 값이 있었고, 그래서 재현되지 않았습니다.
  - 150행으로 바꾼 테스트에서 운영 환경과 같은 오류를 재현했습니다.
- **추가로 발견한 결함:** 헤더만 있고 행이 0개인 월간 파일이 "다운로드 성공 + 한 달 전체 gap"으로 기록됐습니다.
- **수정**
  - 12개 열에 명시적 스키마를 적용합니다.
  - `PolarsError`는 파일 단위로 격리해 실패로 기록하고, 다음 실행에서 다시 시도합니다.
  - 행이 0개인 파일은 실패로 처리합니다.
- **회귀 테스트:** `test_parse_does_not_infer_integer_columns_from_early_rows`, `test_a_corrupt_csv_fails_only_its_own_file`(잘못된 행, 헤더만 있는 파일, 정상 파일)
- **재검증:** 게이트 통과(189 passed). 다운로드를 다시 실행해 이어서 받습니다.
- **상태:** 해결(전체 실행 결과는 progress에 기록)

## F-009 (2026-11-09, 작업 8) 벌크 전체 실행이 메모리 부족 회수로 중단
- **증상:** 1,053개 파일(12/632 심볼)을 처리한 뒤 프로세스가 사라졌습니다. 실패 기록과 오류 로그는 없었습니다. 로그의 마지막 줄은 `[10/632] 1000SHIBUSDT`(10:02)였습니다.
- **원인(확인한 범위)**
  - 다운로드는 Claude Code 세션의 백그라운드 작업으로 실행 중이었고, 시스템 메모리가 부족해지자 세션이 백그라운드 작업을 회수했습니다. 같은 날 관리자 조회 작업도 같은 방식으로 회수됐습니다.
  - 실행 방식(세션 종속)이 직접 원인입니다.
  - 다운로드 프로세스의 실제 최대 메모리는 당시 측정하지 않았습니다(미확인).
  - 재개 테스트에서는 파일을 하나씩 순차 처리했고, 최대 working set은 195MB, private 메모리는 254MB였습니다. 따라서 다운로드 프로세스 혼자 메모리를 고갈시켰을 가능성은 낮습니다.
  - **함께 발견한 사항:** 실시간 수집기 rest 작업 프로세스의 private 메모리가 783MB(working set 30MB)로, 계획 상한 300MB를 넘습니다. 시스템 메모리 압박에 기여했을 수 있습니다. 원인 분석은 별도 과제입니다.
- **수정**
  - 작업 스케줄러 작업(`qr-bulk-download`)으로 옮겨 세션과 분리했습니다. 우선순위는 BelowNormal입니다.
  - `POLARS_MAX_THREADS=2`로 제한하고, 동시 실행은 1로 둡니다.
  - 파일마다 자원 확인을 추가했습니다(`bulk/guard.py`): 가용 RAM 1.5GB 이상, 자기 프로세스 private 메모리 1GB 이하, 디스크 여유 50GB 이상, 벌크 상한 200GB, SSD 온도. 위반하면 재개 가능한 상태로 exit 3 합니다.
  - 단일 인스턴스 잠금을 추가하고, pythonw 환경의 예외 traceback을 로그에 남기도록 했습니다.
- **구현 중 오류:** `K32GetProcessMemoryInfo`가 실패했습니다(Errno 0). `GetCurrentProcess`의 의사 핸들 -1을 ctypes가 32비트 int로 넘겼기 때문입니다. `restype`과 `argtypes`를 명시해 해결했고, 재실행해 확인했습니다.
- **테스트:** `tests/bulk/test_guard.py`(한도별 차단, 경계값, 결합 규칙)
- **실측 검증:** 2중 실행 차단(exit 2), 소규모 재개 테스트(exit 0, 실패 0)
- **상태:** 해결. 전체 실행 결과는 progress에 기록합니다.

## F-010 (2026-11-09, 리뷰) 전체 코드 리뷰로 찾은 데이터 정확성·메모리 결함
- **발견 경위:** 사용자가 지시한 "상세 확인" 단계에서 code-reviewer 서브에이전트로 전체 리뷰를 했고, 핵심 항목은 직접 재현했습니다.
- **#3 (높음, 재현됨) 시점 정합 조회 오류**
  - 증상: 옛 5분 구간의 수정본(나중에 받은 값)이 더 새로운 구간 대신 "최신 값"으로 반환됐습니다. 같은 시각에 받은 행들은 입력 순서에 따라 결과가 달라졌습니다.
  - 재현: 결정 시점 5,000초의 조회 결과가 event 3,000초가 아니라 event 0(v=9)이었습니다.
  - 원인: `access.py`가 `available_ns`만으로 정렬했습니다.
  - 수정: 사용 가능한 행 중 `event_ns`가 가장 큰 행을 반환합니다. 옛 구간의 수정본은 데이터에 남지만 as-of 결과에서는 빠집니다.
- **#4 (중간, 재현됨) lag 우회:** 연구 역할에서 옛(더 작은) lag 버전을 고정할 수 있었습니다. 이제 거부하며, 재현용 `reproduce_archived_run=True`를 명시할 때만 허용합니다.
- **#5 (중간):** float 또는 Datetime 타입의 `decision_ns`가 봉인 검사를 건너뛰었습니다. 이제 정수 타입만 허용합니다.
- **#1 (높음) rest 작업 프로세스 메모리 783MB**
  - 원인: 컴팩션이 세그먼트 전체를 Python 객체로 올렸습니다(원본 바이트, 압축 해제본, payload dict, 행 dict, 프레임이 동시에 메모리에 있음). Polars 스레드 수도 제한하지 않았습니다.
  - 수정
    - 한 줄씩 스트리밍으로 읽고, 10만 행 단위로 묶어 열 형식으로 변환합니다.
    - 컴팩션을 매 실행 짧게 살다 종료하는 spawn 자식 프로세스에서 돌립니다. 수집기 프로세스는 Polars를 import하지 않습니다(정적 import 그래프 테스트로 확인).
    - `POLARS_MAX_THREADS`를 2로 제한합니다.
- **#2 (높음):** 읽을 수 없는 세그먼트 하나가 이후 모든 세그먼트의 컴팩션을 막았습니다. 이제 세그먼트별로 실패를 격리합니다. 압축 작업이 `.gz`로 바꿔 사라진 파일은 실패가 아니라 `vanished`로 집계합니다.
- **#6 (중간):** 벌크 다운로드의 gap 행과 `bulk_file` 행이 서로 다른 트랜잭션이었습니다. `Catalog.transaction()`(`begin immediate`)으로 묶었습니다.
- **수정분 재리뷰에서 나온 결함 4건(모두 수정)**
  - (a) 고아 컴팩션 프로세스와 같은 임시 파일 이름을 공유하는 문제 → 임시 파일 이름에 프로세스 번호(pid)를 붙입니다.
  - (b) `AttributeError`가 격리되지 않던 문제
  - (c) `event_ns`가 null인 행이 사라지던 문제
  - (d) commit 실패 뒤 트랜잭션이 열린 채 남던 문제
- **구현 중 실수 1건:** 패치 스크립트를 heredoc으로 작성했습니다(`\n` 포함). 규칙 위반이며, diff와 ast로 손상이 없음을 확인했습니다.
- **회귀 테스트(먼저 실패를 확인함)**
  - `tests/data/test_access.py`: 수정본, 동시 사용 가능, 종목별, lag 고정, 타입, null event
  - `tests/data/test_compact.py`: 배치 동등성, null 열 타입, 실패 격리, 사라진 파일, 스트림 동등성, 임시 파일 충돌, 예상 밖 예외
  - `tests/capture/test_compaction_process.py`
  - `tests/bulk/test_klines_download.py`(원자성)
  - `tests/catalog/test_catalog.py`(commit 실패)
- **적용(사용자 승인 "A+B+C 수정, 짧게 재시작")**
  - 02:35:28Z에 STOP 파일로 정상 종료했고, 02:35:42Z에 작업 스케줄러가 재시작했습니다.
  - restart gap이 자동으로 기록됐습니다(rest 19.1초, ws 7.1초).
  - rest 작업 프로세스 private 메모리: 717MB(재시작 직전) → 62MB(재시작 직후, 첫 컴팩션 실행 뒤)
- **보류:** #7 카탈로그 DB 백업, #8~#11(잘못된 세그먼트 격리, gz 꼬리, basis 400 소음, 시작 시 꼬리 읽기) → 10단계와 4단계에서 처리합니다.
- **상태:** 해결. 정시가 지난 뒤 실제 컴팩션에서 메모리를 실측한 결과는 progress에 기록합니다.

## F-011 (2026-11-09, 운영) 절전·장애 중 공백이 기록되지 않음, 백업 경쟁 실패
- **증상 1: 기록되지 않은 공백**
  - 노트북이 03:36:10Z~06:45:45Z에 Modern Standby(절전) 상태였습니다(이벤트 506/507). 재부팅은 아니었습니다.
  - 그동안 bookTicker, Upbit 호가, 강제청산 WS에 190분 공백이 생겼지만 gap 기록이 없었습니다.
  - 2026-11-08 17:02~20:48Z Upbit ConnectTimeout 구간도 기록이 없었습니다.
- **원인 1:** gap 기록은 프로세스 재시작(restart)과 API 범위 초과만 다룹니다. 절전 중 프로세스는 죽지 않고 멈췄다가 그대로 이어서 실행되고, 요청이 계속 실패할 때는 아무것도 기록하지 않습니다.
- **증상 2: 백업 실패:** 06:46:26Z에 백업이 `FileNotFoundError`로 실패했습니다.
- **원인 2:** 백업 목록을 만든 뒤 압축 작업이 원본 `.jsonl`을 `.gz`로 바꿨습니다. 컴팩션에서 F-010 #2로 고친 것과 같은 유형입니다.
- **수정**
  - `data/gapscan.py` 추가
    - 1초 주기 소스의 닫힌 세그먼트에서 수신 간격이 30초를 넘는 구간을 gap으로 기록합니다(cause `unknown`, detail `receive_gap`).
    - 세그먼트 경계를 넘는 공백도 잡습니다(마이그레이션 0005 `gapscan_segment`).
    - 컴팩션 자식 프로세스에서 컴팩션 직후 실행합니다.
  - 백업은 사라진 파일을 `vanished`로 건너뜁니다.
- **테스트**
  - `tests/data/test_gapscan.py`(6개): 지터는 공백이 아님, 세그먼트 안 공백, 경계를 넘는 공백, 증분·멱등, 열린 세그먼트와 이벤트성 소스는 스캔하지 않음, 사라진 파일
  - `tests/capture/test_backup.py`(경쟁 상황)
  - `tests/capture/test_compaction_process.py`(자식 프로세스 스캔)
- **적용**
  - 08:26:49Z에 STOP으로 정지했고, 08:27:42Z에 재시작했습니다(공백 53초, restart gap 기록).
  - 08:33Z 첫 스캔: 세그먼트 55개, 소급 gap 20건
    - 절전 190.2분(두 소스 모두)
    - Upbit 장애 174.3분과 50.3분(Upbit 호가만)
    - 11-08 개발 중 재시작과 시험 구간 0.5~7.5분
- **한계**
  - 강제청산 WS는 이벤트성이라 수신 간격으로 판단하지 않습니다. 같은 시간대의 1초 소스 gap으로 판단해야 합니다.
  - 원인(절전, 네트워크, 정지)을 구분하지 못합니다.
  - 임계값 30초는 초깃값이며, 기록된 gap 분포를 보고 다시 조정합니다.
- **상태:** 해결

## F-012 (2026-11-09, 운영) 수집기 restart gap이 카탈로그에 들어가지 않음
- **증상:** 11-08 14:41Z 이후의 `process.rest`·`process.ws` restart gap이 카탈로그 `gap`에 없었습니다. `raw/_gaps/*.jsonl`에만 있었습니다.
- **원인:** 카탈로그로 가져오는 `Catalog.import_gaps`를 bootstrap CLI로만 실행했고, 자동으로 실행하는 곳이 없었습니다.
- **수정:** 컴팩션 자식 프로세스의 `scan_once`가 매 주기 `import_gaps`를 실행합니다. gap identity 인덱스 덕분에 멱등입니다.
  - 테스트: `tests/data/test_gapscan.py::test_scan_once_also_imports_the_collectors_gap_logs_idempotently`
- **적용:** 자식 프로세스는 매번 새로 생성되므로 수집기를 다시 시작하지 않아도 반영됩니다. 이후 restart gap 12건과 10건이 들어온 것을 확인했습니다(`docs/measurements/validate-week.md` §2).
- **상태:** 해결

## F-013 (2026-11-09, 작업 8) bookDepth `percentage` 형식 변화
- **증상:** 파일럿에서 2026-10 bookDepth 파일이 `could not parse '5.00' as dtype i64`로 실패했습니다.
- **원인:** 2023년 파일은 `-5`, 2026년 파일은 `5.00`으로 씁니다.
- **수정:** Float64로 읽고, 두 형식을 모두 담은 테스트를 추가했습니다. 다시 실행한 파일럿은 2개월, 60개 파일이 통과했습니다.
- **상태:** 해결

## F-014 (2026-11-09, Phase 2 9단계) 실데이터 스모크가 메모리 게이트로 중지
- **증상:** 첫 실행이 `PAUSED at trend event x1.0: process memory 2200 MB > 2048 MB`로 중지됐습니다(exit 3).
- **원인:** `load_panel`이 8심볼 × 7개월 × 4개 데이터셋의 1분봉을 한꺼번에 읽었습니다(premium 포함, 템플릿은 premium을 쓰지 않음).
- **수정:** 심볼 단위로 읽고 조립해 메모리에는 한 심볼의 1분봉만 둡니다. 쓰지 않는 데이터셋을 빼는 `datasets` 인자를 추가했습니다. 자원 게이트(연구 작업자 2GB, 계획 §8)는 그대로 둡니다.
- **결과:** 최대 private 메모리 763MB로 통과했습니다. 기존 패널 테스트는 회귀 없이 통과합니다.
- **상태:** 해결

## F-015 (2026-11-10, Phase 3) 실데이터 Director 실행에서 발견한 정책·가짜 LLM 결함
1. **가짜 LLM 계획 계열 오류**
   - 증상: 계획 요청이 `template` 필드로 계열을 보내는데, 가짜 LLM은 `required_family`만 읽어 trend를 반환했습니다. 그래서 trend가 아닌 계열의 계획이 폐기됐습니다.
   - 10사이클 테스트가 이를 잡지 못했습니다(계획 수와 스키마 실패 수를 검사하지 않음).
   - 수정: 가짜 LLM을 고치고, 테스트에 "계획 ≥ 7, 실패 응답 0"을 추가했습니다.
2. **실행 단서를 계열 차단으로 처리**
   - 증상: funding 미모델 표시만으로 거의 모든 계열이 데이터 대기로 차단됐습니다. 모두 차단되자 정책이 전체 허용으로 되돌아가 funding_basis를 다시 골랐습니다.
   - 수정: 데이터 요청이 기록된 실패만 차단하도록 바꿨습니다.
3. **쿨다운·소진 계열을 정책이 계속 선택**
   - 증상: 첫 실행의 6, 8~10번 사이클이 "새 가설 없음"으로 끝났습니다. 재실행에서는 소진된 trend를 계속 골랐습니다.
   - 수정: 쿨다운·소진 계열을 제외하고, 모두 막히면 PAUSED로 갑니다.
4. **쿨다운 해제가 전달되지 않음**
   - 증상: OBSERVE에서 해제한 쿨다운을 HYPOTHESIZE 검사가 모른 채 다시 막았습니다.
   - 수정: 해제 근거를 HYPOTHESIZE에서 확인합니다.
- **테스트(먼저 실패를 확인함):** `test_only_recorded_data_requests_block_a_family_not_modelling_caveats`, `test_the_policy_skips_families_in_cooldown_so_cycles_stay_productive`, `test_a_family_whose_fixed_grid_is_fully_evaluated_is_skipped_as_exhausted`, `test_when_every_family_is_exhausted_or_blocked_the_run_pauses_instead_of_spending_llm_calls`, `test_when_only_cooldowns_block_every_family_the_oldest_cooldown_is_released`
- **상태:** 해결. 최종 실데이터 실행에서 10/10 사이클이 평가까지 진행됐습니다.

## F-016 (2026-11-10, Phase 3 실제 LLM) 실제 모델의 가설 6/6이 스키마 검증 실패
- **증상:** run `cli-s7-on-posterior-20261110`(구독 CLI, 3사이클)에서 가설 호출 6건이 모두 `schema_invalid`였습니다. 3사이클 모두 REPLAN → DONE으로 끝났고 trial은 0건입니다.
  - 1차 시도(3/3): `statement`가 600자를 넘었습니다(701, 849, 609자).
  - 2차 시도(3/3): 특징 이름이 `^[a-z_]{1,32}$` 규칙을 위반했습니다(예: `ret_24h`, `ema_50_close`, `realized_vol_60d`).
- **재현:** `tests/director/fixtures/f016_real_answers.json`(실제 응답 원문 6건). `tests/director/test_schema_contract.py`가 이 6건을 고정합니다.
- **근본 원인(스키마 계약 불일치)**
  1. `api_schema`가 값 제약(maxLength, pattern 등)과 description을 지우고 보냈습니다. 그래서 모델은 1차 시도에서 그 제약을 알 수 없었습니다.
  2. 특징 이름 규칙이 숫자를 허용하지 않았고, 허용 어휘(DSL 연산자)를 알려 주지 않았습니다. 모델은 자유 형식 이름을 썼습니다.
  3. 재시도 피드백은 pydantic 오류 문자열을 300자에서 자른 것이었습니다. 첫 오류만 전달됐고 규칙은 전달되지 않았습니다.
  4. 가짜 LLM은 스키마를 항상 지켰기 때문에, 가짜 LLM 테스트는 이 계약 결함을 잡을 수 없었습니다.
- **수정(커밋 94c5c5f)**
  - 특징 이름은 `^[a-z][a-z0-9_]{0,31}$` 형식과 DSL 문법을 모두 만족해야 합니다. 문법: `<column> | <operator> | <op>_<window>[_<column>] | <xs·regime op>_<column>`(창은 1~2000봉).
    - 저장된 기존 이름은 모두 `[]`이므로 호환됩니다. 기존 테스트 이름 `ema`도 그대로 유효합니다.
    - `_`로 시작하는 이름은 이제 거부됩니다(저장된 사례 0건).
  - 전송 스키마에서 지운 제약은 각 속성의 `description` 문장으로 다시 적습니다. 시스템 프롬프트에는 `constraint_notes`로 모든 필드의 타입, 허용값, 한도를 넣습니다(프롬프트 버전 phase3-v2).
  - 재시도 피드백은 `[{"field", "problem"}]` 목록입니다. 답변 원문은 되돌려 보내지 않고, 익명화 redact를 적용합니다.
  - 로컬 pydantic 검증이 최종 권위입니다. 자르거나 값을 바꿔 통과시키는 처리는 없습니다.
- **테스트(먼저 실패를 확인함):** 실제 응답 6건 회귀, 600/601자 경계, 숫자가 든 DSL 이름 수락, 존재하지 않는 이름과 패턴 위반 거부, 전송 스키마의 제약 설명, 제약 안내 내용, 피드백 후 재생성 성공(가짜 LLM `too_long`, `unknown_feature` 시나리오).
- **상태:** 해결(2026-11-10). 실제 LLM 재검증 run `cli-s7-on-posterior-20261110-r2`에서 LLM 호출 9/9가 1차 시도에 스키마를 통과했고, 3/3 사이클이 BACKTEST까지 진행했습니다(trial 9건, 중복 0). `docs/measurements/phase3-cli-3cycles-r2.md`.

## F-017 (2026-11-19, 운영) WS 수집기가 7일 동안 크래시 루프: 0바이트 heartbeat 파일을 시작 시 파싱 실패
- **증상:** `supervisor.log`에 `ws` child_exited code=1이 2026-11-12 173회, 11-13~11-18 매일 283회(약 5분 주기 재시작), 11-19 85회. `ws.log` 마지막 traceback: `run.py:92 previous_beat_ns` → `json.loads("")` → `JSONDecodeError: Expecting value`. catalog `incident`에 `heartbeat:ws` critical이 2026-11-12 09:42Z부터 열려 있고, 모니터 토스트는 그때 1회만 갔습니다(`run/alerts.log`).
- **발견:** 2026-11-19 세션 시작 점검(`ws.heartbeat.json` 수정 시각 11-12 18:30 KST, 크기 0바이트).
- **재현:** `tests/capture/test_collectors.py::test_previous_beat_tolerates_an_empty_or_corrupt_heartbeat_file`(빈 파일, 잘린 JSON, 객체가 아닌 JSON, 정수가 아닌 at_ns). 수정 전 코드로 3/4 실패(JSONDecodeError 2, AttributeError 1)를 확인했습니다.
- **근본 원인**
  1. `Health.write`가 `write_text`로 제자리 덮어쓰기를 했습니다. 11-12 그 순간 프로세스가 죽거나 전원이 끊겨 0바이트 파일이 남았습니다(같은 시각에 book_ticker 3,407분 공백·clock offset 642ms 경보가 있어 PC 정지로 추정, **미확인**). 설계 §3.1의 "임시 파일에 쓴 뒤 교체" 원칙이 heartbeat에는 적용되지 않았습니다.
  2. `Health.previous_beat_ns`가 손상 파일을 예외로 두어, supervisor가 재시작할 때마다 같은 자리에서 다시 죽었습니다. 모니터(`monitor/checks.py`)는 잘린 heartbeat를 허용하지만 수집기 쪽은 허용하지 않았습니다.
  3. 모니터는 열린 critical incident를 재경보하지 않아 7일 동안 사람이 알지 못했습니다(OPS-004와 같은 부류, 별도 결정).
- **수정(이 커밋):** `previous_beat_ns`는 빈·손상·비객체 파일을 `heartbeat.unreadable` 경고와 함께 `None`으로 처리합니다(첫 실행과 같이 시작 미상 restart gap 기록). `write`는 `.tmp`에 쓰고 `os.replace`로 교체하며, Windows에서 읽는 쪽(모니터)이 잠깐 열어 PermissionError가 나면 그 1회만 제자리 쓰기로 대체합니다.
- **영향:** 강제청산 WS(`binance.um.force_order`, 공개 이력 없음)는 2026-11-12 09:42Z부터 재가동 시각까지 **영구 손실**입니다. REST 수집기(bookTicker, Upbit 호가·체결, 지표)는 같은 기간 동작했습니다(heartbeat `ok` 카운터 기준, 커버리지 수치 대조는 미실시).
- **상태:** 코드 수정 완료(테스트 먼저). 운영 재가동은 저장소 이동 계획 단계 5(예약 작업 재등록, 사용자 실행) 뒤에 확인합니다. 재경보 주기는 OPS-004 해결 계획에 묶어 별도 결정합니다.
- **재가동(2026-11-19 17:05 KST):** 사용자가 `register_collector_task.ps1`을 실행해 새 경로로 등록, Start 후 ws가 `heartbeat.unreadable` 경고 1회 뒤 정상 기동(PID 6724, `force_order` 수신). 해결.

## F-018 (2026-11-19, Phase 4 단계 10) 실데이터 S2의 71%가 "벡터 엔진은 경로 의존 손절을 지원하지 않음"으로 거부
- **증상:** 1차 실데이터 배치(`search-s7-20261119`, 계열 5 × 탐색기 2, S1 200씩)에서 S2 trial 178건 중 127건이 `rejected`, 사유 "vector engine: time stop / ATR stop are path dependent (use the event engine)". 회계는 정확했지만(trial 4,173 = 단계 합) S2 통과율이 의미를 잃었다.
- **원인:** 탐색 공간의 구조 축 `time_stop`(0/24/72/168)이 후보 3/4를 경로 의존 spec으로 만드는데, S2 단계는 선별 구간용으로 벡터 엔진을 고정했다(ADR-020의 벡터 엔진 규칙은 상태 없는 규칙만 지원).
- **수정:** `search/s2_stage.run_s2`가 spec에 시간 손절·ATR 손절이 있으면 이벤트 엔진으로 자동 전환하고 `engine_used`를 metrics에 남긴다. 테스트 `test_stateful_specs_are_evaluated_by_the_event_engine_at_s2`(수정 전 코드로는 거부 사유에 "path dependent"가 남아 실패).
- **영향:** S2 비용이 늘었다(이벤트 엔진). 2차 배치(`search-s7-20261119-r2`)로 측정표를 다시 만들었다. 1차 배치의 trial은 그대로 남아 N_total에 포함된다(재평가는 새 trial, §9-2).
- **상태:** 해결(2026-11-19).

## F-019 (2026-11-20, 독립 리뷰) Phase 4~7 변경분 리뷰에서 확인된 결함 (`code-reviewer` 2회: 3bc2950..1345643, A1 diff)
- **증상과 원인(모두 코드 추적 또는 실행으로 확인, 테스트로 재현 후 수정):**
  1. 모의매매 루프가 매 tick 같은 policy.json을 다시 적용 → 두 번째부터 "rollback refused", tick 미실행. 원인: 이미 적용된 정책을 건너뛰지 않음.
  2. holdout lag 스트레스가 config만 바꾸고 base latency로 조립된 frame을 재사용 → lag+1·lagx2 SR이 항상 base와 같음(6 spec 실측). 원인: 체결 가격은 조립 시점에 계산된다.
  3. 같은 fold로 재시작하면 S0가 모든 제안을 duplicate로 거부 → S1·S2 0건, best_s2 None. 기존 resume 테스트는 trial 수만 보고 반환값을 버려 통과했다.
  4. 여러 전략이 심볼별 포지션 하나를 공유 → 반대 방향 전략이 서로 되돌림, 빠진 전략의 포지션을 남이 청산.
  5. 한 tick의 주문을 모두 tick 시작 노출로 검사(8×4,000이 gross 20,000 통과), 미체결 주문 미포함, 주문 속도 한도 0 고정, 손실 창이 누적 실현 손익 하나.
  6. 체결 도착 시각 = 결정 시각 + 지연 → 10:05 주문이 09:01 호가로 체결(약 1시간 과거 체결).
  7. client id의 seq가 필터를 통과한 순번 → 다른 심볼이 빠지면 id가 바뀜.
  8. validator 예산 k가 판정 규칙과 묶이지 않음(k=1 규칙으로 k=20 예산 사용 가능).
  9. 낮음: 정책 키 없을 때 빈 HMAC 키(fail-open), 월 단위 LLM cutoff를 그 달 1일로 해석, lifecycle 상태 확인이 쓰기 잠금 밖, ack의 venue id 갱신이 트랜잭션 밖, S2 resume 조회가 이벤트 엔진 전환을 무시, live PENDING_SUBMIT가 크래시 후 재제출 가능, 서명 패키지 kind·미래 발행 시각 미검사.
  10. A1 리뷰: 새 체결 창 테스트와 기존 tick 테스트가 주문이 0건이라 핵심 단언이 실행되지 않음(데이터 60시간 < 48시간 z-score 웜업 + 결정 창 1일).
- **수정:** 1 `load_policy`가 적용 중인 정책과 같으면 건너뜀, 키 없으면 `RefuseAllVerifier`. 2 `synthetic_splits`·`holdout_evaluation(panel_at_latency=)`로 지연별 재조립, 없으면 거부. 3 `S0Outcome.duplicate_of` + `reuse_s1`(같은 fold·data_version·S1 설정의 done trial 재사용), 테스트가 재시작 best_s2 = 1회차를 단언. 4 `ledger.strategy_positions`·`plan_portfolio`(전략별 포지션, 승인 해제 전략은 자기 해시로 청산, 전략당 자본 균등). 5 `risk.limits.approve_in_order`(누적 노출), `account_exposure`(미체결 포함, 최근 1분 주문 수, 실현+미실현 손실 창). 6 도착 = max(제출, 결정) + 지연. 7 심볼별 seq 0 고정. 8 마이그레이션 0003: `query_budget.rule_hash`, register·judge가 규칙 해시 대조. 9 각 항목 수정(월 말일, `begin immediate` 안에서 상태 확인, `_move(also=)`, `s2_engine_config` 공용, `send_attempted`, `expected_kind`·시계 오차 60초). 10 테스트 데이터 100시간·평균회귀·equal sizing으로 주문이 실제로 생기게 하고 전제를 단언. 추가로 lifecycle 연결(Validated→Paper, 일 1회 `paper_health`로 CUSUM 저하 판정, 기대치는 제출 실험의 S3 일수익률).
- **이월(계획 단계로):** holdout 접근 로그 강제·1비트 격리(별도 ack 파일)·해시 체인 꼬리 절단 앵커 → B8(validator 프로세스). 단일 질의 FWER 측정(복제당 질의 1회는 종단 FWER가 아님) → A3. ONC 범위(k≥2, ≤30) 보고서 명시 → A12. 전략별 슬리피지·gap 비율 기록 → B11.
- **상태:** 수정(2026-11-20), 각 영역 테스트 통과. 게이트·CI는 커밋 시 기록.
- **2차 리뷰(수정 diff 전체, 2026-11-20) 추가 확인·수정:** (a) lake 지연(시간 세그먼트, 시간 종료 1분 뒤부터 압축, 10분 주기 → 61~71분) 때문에 벽시계 기준 취소가 모든 모의 주문을 체결 전에 취소 → `MarketFeed.coverage_ns()`가 체결 창 전체를 덮을 때 한 번만 체결 시뮬레이션하고 남은 수량 취소(같은 호가를 tick마다 다시 쓰던 문제도 해소), (b) `daily_returns_from_fills`가 창 이전 체결을 버리고(부호 반전) 결측 종가로 이동분을 잃음 → 첫날은 기준일(보유만 쌓음), 마지막 종가 승계, (c) 미체결 주문을 다시 계획해 노출 이중 계산 → 계획에 미체결 잔량 포함, 이미 있는 client id는 위험 검사에서 제외, (d) 빈 패널 가드가 발동 불가(심볼 × 결정 cross join) → 결정 시각까지 오는 봉이 없는 심볼은 가격 제외, 전부 묵으면 결정 안 함, (e) health·승격·자본이 매매 대상(시간봉)과 다른 목록 → 같은 목록, (f) 미완료 holdout verdict에 rule_hash 없음 → `incomplete_verdict`, (g) S1 재사용 키에 slice 창·합성 spec 없음 → metrics `window`, 합성 패널 파일 식별자에 spec, (h) rule NULL 예산 행 재등록 경로 없음 → 사용 0일 때 1회 바인딩. 테스트 가짜 피드는 tick 시각 이후 호가를 볼 수 없게(`visible_until`) 고쳤다.
- **3·4차 리뷰(2026-11-20) 추가 수정:** (a) 최신 lake 파일만으로 체결 창 커버를 판단하면, 압축이 실패한 앞 시간 세그먼트를 건너뛴 경우 호가 0건으로 거짓 취소 → `LakeFeed.covers(window)`: 창과 겹치는 모든 raw 세그먼트가 lake 파일(같은 stem) 또는 catalog `compacted_segment` 기록을 가져야 함(0행 세그먼트는 lake 파일 없이 기록만 남는다: 수집기 재시작의 헤더만 있는 세그먼트), (b) `reuse_s1`이 가장 오래된 trial만 비교 → 창이 일치하는 첫 trial, (c) `reduce_only`를 미체결 포함 수량으로 판단 → 체결 수량으로, (d) 일일 health가 lake 지연으로 잘린 전날로 실행될 수 있음 → `health_due`가 전날 마지막 시간 커버를 확인, (e) `judge`가 제출 규칙과 epoch 규칙을 대조. 3차 수정은 테스트를 수정과 함께 추가해 실패 확인을 따로 하지 않았고, 4차(0행 세그먼트)는 실패를 먼저 확인했다.

## F-020 (2026-11-21) 캠페인 c1이 연구 게이트(프로세스 메모리 2GB)에 두 번 멈춤
- **증상:** 49배치 뒤 2,260MB, 재개 3배치 뒤 2,332MB로 `status: gate`.
- **재현·측정(프로브):** 같은 셀을 임시 DB로 돌리면 900MB다. `low_vol@1d|top100|w1` 한 배치에서 +1GB가 늘었고, 그 안에서 `select_top_liquidity`만으로 654MB → 4,454MB가 됐다.
- **근본 원인:** 직전 30일 거래대금을 (심볼, 날짜) × (심볼, 모든 봉 날짜) 조인 후 필터로 구했다. 날짜 수의 제곱이 된다(155심볼 × 1,100일² ≈ 1.9억 행). 6개월 창에서는 드러나지 않았고, 36개월 1d 창에서 드러났다.
- **수정:** 누적합의 차(as-of 조인 2회)로 구한다. 구식 구현을 참조로 둔 동일성 테스트(무작위 6개, 정수 거래대금)를 통과한다. 같은 프로브에서 +147MB다.
- **부수 효과:** panel.py가 패널 저장소 키의 소스 해시에 들어 있어 캐시가 다시 만들어진다(창당 약 10분).
- **리뷰 반영:** NaN·inf 거래대금은 0으로 센다. 누적합에서는 비유한 값 하나가 그 심볼의 이후 모든 날을 오염시켜 매일 선택됐다. 회귀 테스트를 넣었고 수정 전 코드에서 실패했다. 참조 비교 테스트에 선택 행 수 > 0 검사를 추가했다.

## F-021 (2026-11-23, v2.4 단계 2) 캠페인 보고서의 "S3 판정 78"과 "unstable 59"가 맞지 않음
- **증상:** c1 보고서에서 확인 구간 판정은 78건인데 확인 단계 라벨(`unstable`)은 59건이었다. 확인 SR이 양수인 18건을 가리키는 라벨도 없었다.
- **원인(코드 경로):** S3는 선택 구간 SR의 부호와 무관하게 노출 조건을 넘은 최선 1개에 대해 돈다(`search/pipeline.py`의 `s3_top`). 라벨은 `classify`가 앞 단계 사유부터 붙인다(`search/campaign.py`). 그래서 S3가 돈 배치 중 19건이 `no_signal`(17)·`cost_dominated`(2)로 기록됐다. 78 = 59 + 17 + 2.
- **두 번째 원인:** 확인 SR > 0인데 후보가 아닌 배치가 확인 SR ≤ 0과 같은 `unstable`로 기록됐다.
- **수정(ADR-050):** `below_bar`(확인 SR > 0, 결합 확인 미충족, 하위 사유 3개)를 더했다. `unstable`은 확인 SR ≤ 0만 뜻한다. `relabel`이 저장된 요약에 현재 규칙을 다시 적용한다(평가 재실행 없음).
- **재집계(`docs/measurements/v24-relabel.md`):** 103배치. `unstable` 59 → `unstable` 45 + `below_bar` 14(모두 `no_cross`: 교차 측정이 생기기 전 배치). 확인 SR 양수 18 = `below_bar` 14 + `no_signal`로 기록된 4.
- **함께 측정:** `sparse` 배치의 S2 점 123개 중 45개(37%)가 ADR-051 lookback 상한을 넘는다. `low_vol@1d|top100|w2`는 8개 전부다.
- **남은 것:** 앞 단계 사유로 끝난 배치의 S3는 계속 누적 N에 들어간다(의도된 동작, ADR-050 결정 4).
