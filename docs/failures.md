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
- **시도 횟수:** 1
- **상태:** 해결(재실행 결과 확인 후 확정)
