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
- **수정:** (코드 리뷰 결과와 함께 반영) 마지막 기록 후 10초가 지나면 메시지가 오더라도 heartbeat를 씁니다. 회귀 테스트를 추가합니다.
- **상태:** 수정 중
- **시도 횟수:** 1
- **상태:** 해결(재실행 결과 확인 후 확정)
