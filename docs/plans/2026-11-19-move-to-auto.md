---
status: in-progress
task: move-to-auto
---

# quant-research 저장소를 C:\dev\auto\code로 이동하고 plan·report 폴더로 정리

- 저장 위치: `C:\dev\quant-research\docs\plans\2026-11-19-move-to-auto.md` (이동 뒤에는 `C:\dev\auto\code\docs\plans\…`)
- 승인 근거(사용자 결정 2026-11-19, 그대로 인용): "기존에 … 2026-11-08-autonomous-quant-research.md 를 계획을 하고 어느정도 구현이 진행된상태다 정리하기위해 계획 상세히 확인후 관련된 파일이나 폴더를 전부 C:\dev\auto안에 한곳에 모아라 대신 막집어넣는게 아닌 코드는 code 폴더를 따로만들고 문서는 report폴더에 계획은 plan 폴더를 만들어 깔끔하게 정리해라" / 방식 질문에 "저장소 이동 + 바로가기 폴더 (Recommended)" 선택.
- push: 하지 않는다(quant-research에는 이미 push되지 않은 커밋 7개가 있어 사용자가 따로 결정한다).

## Context
설계 기준서(`docs/plans/2026-11-08-autonomous-quant-research.md`, 893줄, status design)의 `## 변경 범위`는 `C:/dev/quant-research/**`와 데이터 `C:/dev/qr-data/**`다. 즉 "관련 파일"은 저장소 전체(코드 `src/`·`tests/`·`tools/`, 문서 `docs/*.md` 7종 + `docs/measurements/`, 계획 9개, 커밋 78개, 2026-11-08~10)이고, 데이터는 저장소 밖 `C:\dev\qr-data`에 있다. 진행 상태: Phase 0~1 구현·검증 완료(운영 검증 OPS-001~007 미완), Phase 2·3 done, Phase 4 in-progress(미커밋 파일 5개: `search/promotion.py`·테스트 신규, 계획 문서 3개 blocked 표시). 수집기 예약 작업 `qr-collector-supervisor`가 `C:\dev\quant-research\.venv\Scripts\pythonw.exe`로 **실행 중**이고 `qr-bulk-download`(Ready)도 같은 경로를 쓴다. 등록 스크립트 5개의 `$Project` 기본값과 `docs/operations.md` 4곳이 옛 경로를 적고 있다. `C:\dev\auto`는 비어 있는 폴더로 이미 있다(2026-11-19 15:42 생성). `C:\auto`(옛 Upbit 스크립트 예약 작업이 가리키는 곳)는 존재하지 않으며 이 작업과 무관하다.

## 목표
1. `C:\dev\quant-research` 저장소를 **통째로** `C:\dev\auto\code`로 이동한다(git 이력·미커밋 변경·원격 그대로; `docs/plans`는 저장소 안에 남아 hook이 계속 읽는다).
2. `C:\dev\auto\plan` → `code\docs\plans`, `C:\dev\auto\report` → `code\docs` 를 가리키는 Windows junction을 만들어 code/plan/report 세 폴더 모양을 갖춘다(사본 없음, 단일 원본). `C:\dev\auto\README.md`에 배치를 적는다.
3. 수집기 예약 작업 2개를 새 경로로 다시 등록하고 수집기를 다시 실행 상태로 둔다. 코드·문서의 옛 경로 기본값을 새 경로로 바꾼다.
4. 새 위치에서 선언된 게이트가 통과함을 확인한다.

비목표: 저장소 내부 구조 변경(src/tests/tools 재배치 없음), 데이터 `C:\dev\qr-data` 이동 없음, 미커밋 Phase 4 작업의 처리(그대로 두고 이동만), push, claude-config 문서(가이드·백로그)의 경로 갱신(별도·1줄 작업).

## 확인한 사실
- 예약 작업: `qr-collector-supervisor`(Running) = `C:\dev\quant-research\.venv\Scripts\pythonw.exe -m quant_research.capture.run supervisor --log-dir C:\dev\qr-data\logs`; `qr-bulk-download`(Ready) = 같은 pythonw로 `tools\bulk_download.py --universe C:\dev\qr-data\bulk\universe-20261109.json …`. 등록 스크립트 `tools/register_collector_task.ps1`·`register_bulk_task.ps1`는 `Register-ScheduledTask -Force`(재실행 안전)이고 `$Project` 기본값이 `C:\dev\quant-research`.
- 옛 경로가 적힌 곳: `tools/register_collector_task.ps1:13`, `register_bulk_task.ps1:16`, `register_bulk_extras_task.ps1:15`, `register_upbit_backfill_task.ps1:15`, `register_ssd_monitor_task.ps1:12`(스크립트 경로 기본값), `docs/operations.md:7,69,137,138`. `src/quant_research/config.py:17`의 `data_root`는 데이터 경로(`C:/dev/qr-data`)라 바꾸지 않는다. README·CLAUDE.md는 저장소 경로를 적지 않는다.
- `.venv/pyvenv.cfg`의 `home`은 uv 관리 인터프리터(저장소 밖)라 이동 뒤에도 유효하다; 콘솔 스크립트 shim은 절대 경로를 품으므로 이동 뒤 `uv sync`로 복구한다.
- hook: 계획 승인은 세션 폴더의 git 루트에 묶인다. 이동 뒤 루트 경로가 바뀌므로 승인은 이어지지 않는다 → 파일 편집은 모두 이동 전(단계 1)에 끝내고, 이동 뒤에는 편집 없이 파일 시스템·예약 작업·검증만 한다(마감 기록 1파일은 warn 모드의 5개 한도 안).
- Stop hook은 사라진 저장소 `C:\dev\quant-research`를 "미확인"으로 알릴 것이다 → 사용자가 `ack-missing C:\dev\quant-research`로 해제.

## 선택한 접근
사용자가 고른 "저장소 이동 + junction". 대안(저장소 루트를 `C:\dev\auto`로 재구성, 사본)은 질문에서 제시해 기각됐다. junction은 `cmd /c mklink /J`(관리자 권한 불필요)로 만들고, 지울 때는 `rmdir`(링크만 제거).

## 변경 범위
- `tools/register_collector_task.ps1`
- `tools/register_bulk_task.ps1`
- `tools/register_bulk_extras_task.ps1`
- `tools/register_upbit_backfill_task.ps1`
- `tools/register_ssd_monitor_task.ps1`
- `docs/operations.md`
- `docs/decisions.md` (ADR 1건: 저장소 위치)
- `docs/plans/**`

## 데이터 변경 범위
- `C:/dev/quant-research/**` → `C:/dev/auto/code/**` (폴더 이동 1회)
- `C:/dev/auto/plan`, `C:/dev/auto/report` (junction 생성), `C:/dev/auto/README.md`
- Windows 예약 작업 `qr-collector-supervisor`(정지 → 재등록 → 시작), `qr-bulk-download`(재등록)
- 건드리지 않음: `C:/dev/qr-data/**`, `C:/dev/qr-backup/**`, 미커밋 Phase 4 파일의 내용

## 가정
- 이동은 같은 볼륨(C:) 안이라 즉시 끝난다. 수집기를 멈춘 뒤 이동하므로 잠긴 파일이 없다. 수집 공백은 수 분이며 설계대로 재시작 때 gap으로 등록된다.
- `qr-bulk-download`는 Ready 상태(실행 중 아님)라 재등록만 하고 시작하지 않는다(원래 트리거대로 돈다).

## 단계
- [x] 1. (이동 전, 이 저장소에서) 옛 경로 기본값을 `C:\dev\auto\code`로: `tools/register_*.ps1` 5개, `docs/operations.md` 4곳; `docs/decisions.md`에 ADR "저장소를 C:\dev\auto\code로 이동(2026-11-19)" 추가. 선언된 게이트 `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q --cov`를 루트에서 그대로 실행 → exit 0 → 이 파일들 + 이 계획만 커밋(미커밋 Phase 4 파일은 건드리지 않음). — 검증: 게이트 exit 0, `git status`에 Phase 4 파일만 남음.
- [ ] 2. 수집기 정지: `Stop-ScheduledTask qr-collector-supervisor` → `Get-ScheduledTask`가 Ready, `Get-Process pythonw`에 저장소 경로의 프로세스 없음. 정지 시각 기록. (이 저장소 Gotchas: 예약 작업 등록·조작은 auto 모드 분류기가 거부할 수 있다 → 거부되면 사용자에게 `! powershell -NoProfile -Command "Stop-ScheduledTask qr-collector-supervisor"`를 건네고 확인 뒤 진행.)
- [ ] 3. 이동: `Move-Item C:\dev\quant-research C:\dev\auto\code` → `C:\dev\auto\code`에서 `git status`(미커밋 5개 그대로)·`git remote -v`·`git log -1` 확인 → `uv sync`(venv shim 복구). — 검증: `C:\dev\quant-research` 없음, `C:\dev\auto\code\.git` 있음.
- [ ] 4. junction·README: `cmd /c mklink /J C:\dev\auto\plan C:\dev\auto\code\docs\plans`, `cmd /c mklink /J C:\dev\auto\report C:\dev\auto\code\docs`; `C:\dev\auto\README.md`(배치·원본 위치·junction 설명, 10줄 안). — 검증: `dir C:\dev\auto`에 `<JUNCTION>` 2개, `ls C:\dev\auto\plan`이 계획 10개를 보임.
- [ ] 5. 예약 작업 재등록(**사용자 실행**, 이 저장소 Gotchas 2026-11-09: `Register-ScheduledTask`는 auto 모드 분류기가 persistence로 거부): 먼저 `-WhatIf`로 dry-run해 보이고, 사용자에게 다음 두 줄을 건넨다 — `! powershell -NoProfile -File C:\dev\auto\code\tools\register_collector_task.ps1` 와 `! powershell -NoProfile -File C:\dev\auto\code\tools\register_bulk_task.ps1`(시작은 등록 스크립트 또는 `! powershell -NoProfile -Command "Start-ScheduledTask qr-collector-supervisor"`). — 검증(사용자 실행 뒤): `Get-ScheduledTask` 2개의 Action 경로가 `C:\dev\auto\code\.venv\…`, 수집기 State Running, `C:\dev\qr-data\logs`의 supervisor 로그가 1분 안에 갱신됨.
- [ ] 6. 새 위치 검증: `cd C:\dev\auto\code`에서 선언된 게이트를 그대로 실행 → exit 0. — 검증: 게이트 exit 0.
- [ ] 7. 마감: 이 계획 `## 진행 기록`과 `status: done`(이동된 저장소 안, 편집 1파일) → 커밋(push 없음). 사용자 안내: `ack-missing C:\dev\quant-research`, 새 세션은 `C:\dev\auto\code`에서 시작, claude-config 가이드·백로그의 경로 1줄 갱신은 별도.

## 리스크와 대응
- 이동 중 파일 잠김(수집기·에디터·Defender) → 수집기를 먼저 멈추고, `Move-Item` 실패 시 원인 프로세스를 보고하고 중단(강제 종료는 사용자 결정).
- 수집 공백 → 수 분; 설계의 gap 자동 등록. 정지·시작 시각을 기록에 남긴다.
- junction을 `rm -rf`로 지우면 원본 docs가 지워질 수 있다 → 지울 때는 `rmdir C:\dev\auto\plan`만. README에 명시.
- 되돌리기: `Stop-ScheduledTask` → `Move-Item C:\dev\auto\code C:\dev\quant-research` → 등록 스크립트를 `-Project C:\dev\quant-research`로 재실행 → 단계 1 커밋 revert.

## 영향 범위
- 저장소 경로가 바뀐다: 새 세션은 `C:\dev\auto\code`에서 시작. claude-config의 가이드(`docs/guides/working-in-a-new-session.md` §7)와 백로그가 `C:\dev\quant-research`를 언급 — 별도 1줄 갱신.
- 예약 작업 2개의 실행 파일 경로. 데이터 경로는 그대로.

## 진행 기록
<!-- /implement appends one line per step: date, step, commit sha, deviations -->
- 2026-11-19 단계 1: `tools/register_*.ps1` 5개의 `$Project`/`$Script` 기본값, `docs/operations.md` 4곳 → `C:\dev\auto\code`; `docs/decisions.md` ADR-026. 옛 경로는 pyc 캐시에만 남음. 게이트 exit 0(ruff·format·pyright 0·607 passed, 94.24%). Phase 4의 미커밋 파일 5개는 건드리지 않음.
