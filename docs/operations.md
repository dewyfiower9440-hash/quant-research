# 운영 문서 (T0 원본 수집)

## 1. 상시 실행
| 항목 | 내용 |
|---|---|
| 실행 주체 | Windows 작업 스케줄러 `qr-collector-supervisor`. 일반 사용자(`LogonType Interactive`, `RunLevel Limited`), 저장된 비밀번호 없음 |
| 등록·갱신 | `powershell -File C:\dev\auto\code\tools\register_collector_task.ps1`. 다시 실행해도 안전하며, 실행 중인 인스턴스는 유지됩니다 |
| 트리거 | 로그온할 때 + 1분마다 반복(F-006, 2026-11-08 결정 6). 인스턴스가 살아 있는 동안에는 `IgnoreNew` 때문에 반복 트리거가 새 프로세스를 띄우지 않습니다 |
| 프로세스 | `pythonw.exe -m quant_research.capture.run supervisor --log-dir C:\dev\qr-data\logs`. 그 아래 자식 `rest`(REST 폴러)와 `ws`(forceOrder WS)가 있습니다 |
| 로그 | `C:\dev\qr-data\logs\{supervisor,rest,ws}.log`(JSON 줄) |
| 상태 | `C:\dev\qr-data\run\{rest,ws}.heartbeat.json`(10초 주기), incident는 `raw/collector.incidents.{rest,ws}` |
| 중복 방지 | 작업 설정 `IgnoreNew` + 프로세스별 잠금 `run\*.lock`. 수동으로 실행하면 exit 2로 거부됩니다 |

## 2. 제약 (중요)
- **로그인해야 수집이 재개됩니다.** 재부팅하거나 로그아웃하면 다음 로그인 때까지 수집이 멈춥니다. 그 구간은 gap(`process.*`, cause `restart`)으로 기록됩니다.
  - 로그인 없이 실행하려면 S4U, 비밀번호 저장, 또는 관리자 등록이 필요하며 **승인 범위 밖**입니다.
- **로그인 후 자동 재개는 미검증입니다**(2026-11-08 결정 5). PC는 사용자의 명시적 승인 없이 재부팅하지 않습니다.
  - 검증 방법: 재부팅하고 로그인한 뒤 다음을 확인합니다.
    - (1) `Get-ScheduledTaskInfo qr-collector-supervisor`의 `LastRunTime`이 로그인 시각인지
    - (2) `raw/_gaps/process.rest.jsonl` 마지막 줄의 `end_ns`가 로그인 직후인지
    - (3) heartbeat 파일의 나이가 10초 이하인지
- **장애 복구 지연(실측):** supervisor 프로세스 트리가 통째로 죽은 경우 46초(1분 트리거, 2026-11-09). 자식 하나만 죽으면 supervisor가 backoff 10초부터 재시작합니다.

## 3. 정지와 재시작
- **완전 정지:** 다음 순서를 지켜야 합니다(순서가 바뀌면 1분 안에 다시 시작됩니다).
  1. `Disable-ScheduledTask qr-collector-supervisor`
  2. `New-Item C:\dev\qr-data\run\STOP` 생성 → 자식이 세그먼트를 닫고 정상 종료하며, supervisor도 종료됩니다.
- **재개:** `STOP` 파일을 지운 뒤 `Enable-ScheduledTask qr-collector-supervisor`, 그리고 `Start-ScheduledTask qr-collector-supervisor`
- **코드 갱신 반영:** 위 순서대로 완전 정지 → `git pull` → `uv sync` → 재개. 원본 헤더의 `git_sha`로 어떤 코드가 수집했는지 구분됩니다.

## 4. 저장·백업
- **원본:** `C:\dev\qr-data\raw\<source>\<UTC날짜>\*.jsonl`(현재 시간대) / `*.jsonl.gz`(시간대가 끝나고 10분 뒤 압축, sha256 검증 뒤 원본 삭제, ADR-006)
- **백업(임시):** `C:\dev\qr-backup`(같은 SSD, 매시간 증분, sha256 manifest, ADR-008). **SSD 고장에는 대비하지 못합니다.** 외부 백업은 OneDrive 조건부 승인 상태입니다(§6).
- **복원 리허설:** `uv run python -c "from pathlib import Path; from quant_research.capture.backup import restore_and_verify; print(restore_and_verify(Path('C:/dev/qr-backup'), Path('<임시 경로>')))"`(2026-11-08 실데이터 72/72 일치)

## 5. 카탈로그
- `C:\dev\qr-data\meta\catalog.db`(SQLite WAL). 초기화와 gap 가져오기: `uv run python -m quant_research.catalog.bootstrap`(멱등)
- 봉인 경계와 lag 정책은 수정·삭제할 수 없습니다(SQL 트리거). 변경은 새 epoch나 새 version을 추가하는 방식으로만 하며, lag는 늘리는 방향으로만 바꿀 수 있습니다.

## 6. 벌크 다운로드 작업 (작업 8)
- **작업:** `qr-bulk-download`. 등록은 `tools/register_bulk_task.ps1`로 하며, 일반 사용자 권한이고 로그인 상태에서만 실행됩니다.
  - 30분마다 시작을 시도합니다(7일 동안). 실행 중이면 무시됩니다(IgnoreNew와 `run/bulk.lock`).
- **상태 확인**
  - 진행 상태: `C:\dev\qr-data\run\bulk.progress.json`(state, weighted_progress, eta_s, totals, memory_mb)
  - 로그: `C:\dev\qr-data\logs\bulk-download-task.log`
- **종료 코드:** 0 완료, 2 이미 실행 중, 3 자원·온도 차단으로 중지(다음 주기에 재개), 4 실패 파일 있음(다음 주기에 재시도), 1 오류
- **일시 정지:** `Disable-ScheduledTask qr-bulk-download`
  - 실행 중인 프로세스를 끝내려면 `Stop-ScheduledTask qr-bulk-download`를 실행합니다.
  - 파일 단위로 기록하므로 중간에 강제 종료해도 다음 실행에서 이어 받습니다.
- **완료 후:** `bulk/complete-universe-20261109.json`이 생기면 `Unregister-ScheduledTask -TaskName qr-bulk-download -Confirm:$false`로 작업을 지웁니다.

## 7. 절전·공백
- **절전 영향:** 노트북이 절전(덮개 닫힘, 유휴)에 들어가면 수집기 프로세스가 멈춥니다. 그동안의 실시간 데이터(bookTicker, 호가, 강제청산)는 **영구히 손실**됩니다.
- **자동 복구:** 깨어나면 프로세스가 그대로 이어서 실행됩니다. Upbit 체결(7일 범위)과 Binance 지표(30일 범위)는 자동으로 따라잡습니다.
- **공백 기록:** 이 공백은 rest 작업 프로세스의 컴팩션 주기(10분) 안에 수신 공백 스캔이 gap으로 기록합니다(`gap.detail = 'receive_gap'`). 공백이 들어 있는 시간대의 세그먼트가 닫힌 뒤에 기록됩니다.
- **조회:** `select source, start_ns, end_ns from gap where detail = 'receive_gap' order by start_ns`
- **권장(사용자 결정):** 수집 중에는 전원을 연결하고 설정 → 시스템 → 전원에서 "전원 연결 시 절전 안 함"으로 두세요. 이 SSD는 절전 중 BSOD 이력이 있습니다. Claude는 OS 전원 설정을 바꾸지 않습니다.

## 8. 모니터와 알림 (작업 10, ADR-017)
- rest 작업 프로세스가 5분마다 점검합니다. 새 incident는 `C:\dev\qr-data\run\alerts.log`에 남고 Windows 토스트로 뜹니다.
- **조회:** `select key, severity, opened_ns, closed_ns, detail from incident order by opened_ns desc`
- **현재 열린 incident:** `ssd_monitor`. SSD 감시 작업이 등록되지 않아서이며, 등록하면 닫힙니다.

## 9. 백업과 복원 리허설
- 백업 job(1시간 주기)이 원본을 증분 복사하고, 카탈로그 스냅샷(`C:\dev\qr-backup\catalog\catalog-YYYYMMDD.db`, 7일)을 만듭니다.
- **리허설:** `uv run python tools/restore_drill.py --to C:/dev/qr-restore-drill --snapshot` → exit 0이면 전부 일치입니다. 복원 폴더는 자동으로 지웁니다.

## 10. T1 추가 벌크와 유니버스 적재
- **등록(사용자 실행):** `powershell -NoProfile -File C:\dev\auto\code\tools\register_bulk_extras_task.ps1`(작업 `qr-bulk-extras`)
  - klines가 실행 중이면 exit 2로 끝나고 30분 뒤 다시 시도합니다.
- **상태:** `run/bulk-extras.progress.json`, `logs/bulk-extras-task.log`. 완료되면 `bulk/complete-extras-universe-20261109.json`이 생깁니다.
- **유니버스 적재(멱등):** `uv run python tools/load_universe_catalog.py --universe C:/dev/qr-data/bulk/universe-20261109.json`
- **벌크와 REST 대조:** `uv run python tools/validate_bulk_klines.py [--seed N]`
- **7일 검증 보고:** `uv run python tools/validate_week.py`

## 13. Phase 3 Director 실행
- **무료 실행(가짜 LLM):** `uv run python tools/run_director.py --cycles 10 [--seed N] [--universe-n N] [--memory off] [--policy uniform]`
- **유료 실행(승인 후에만):** `--llm claude --approve-paid-calls --max-total-usd 5`
  - 누적 청구 가능 비용은 research.db `llm_call.usage_cost_usd`(billed=1)의 합입니다.
- **조회**
  - 전이: `select c.seq, t.from_state, t.to_state, t.reason from transition t join cycle c using (cycle_id) where c.run_id = ?`
  - 비용: `select purpose, count(*), sum(estimate_usd), sum(usage_cost_usd) from llm_call group by 1`
- **잠금과 게이트:** `run/research.lock`으로 하나만 실행합니다. 자원·온도 게이트에 걸리면 PAUSED로 멈추고, 다음 실행이 그 사이클을 이어 갑니다.

## 12. Phase 2 연구 실행
- **연구 DB:** `C:\dev\qr-data\meta\research.db`(experiment, trial, backtest_run). 쓰는 쪽은 research 프로세스 하나이며 `run/research.lock`으로 보장합니다. catalog.db에는 쓰지 않습니다.
- **스모크:** `uv run python tools/run_backtest.py [--symbols ...] [--from 2025-01] [--to 2025-06]`
  - 연구 구간만 쓸 수 있고, 봉인 경계에 닿으면 거부합니다.
  - 자원·온도 게이트에 걸리면 exit 3으로 멈춥니다.
- **조회:** `select stage, status, count(*) from trial group by 1, 2`
- **원칙:** 모든 평가는 trial입니다(거부·실패 포함). 백테스트 수익률만으로 성공을 판단하지 않습니다(validator는 Phase 5).

## 11. 이월 운영 이슈 (Phase 1 → Phase 2, 2026-11-09)
Phase 1은 **IMPLEMENTED / OPERATIONAL_VALIDATION_PENDING**입니다(progress 머리말). 아래 이슈는 해결 기준을 충족할 때만 닫습니다. 상태 값은 OPEN / IN_PROGRESS / BLOCKED(사용자) / CLOSED이며, 닫을 때는 증거(명령 출력, run id)를 적습니다.

### OPS-001 절전으로 인한 실시간 데이터 공백
- **상태:** OPEN(사용자 결정 대기). Claude는 OS 전원 설정을 바꾸지 않습니다.
- **영향 범위:** bookTicker 1초, Upbit 호가 1초, 강제청산 WS는 공백이 영구 손실입니다. Upbit 체결(7일)과 Binance 지표(30일)는 자동으로 따라잡습니다.
- **위험도:** 높음
  - 2026-11-09에 두 번 있었습니다(190분, 83분). 7일 검증 커버리지가 82%까지 떨어졌습니다.
  - 이 SSD는 절전 중 BSOD 이력이 있습니다.
- **임시 대응책:** 수신 공백 스캔이 gap(`receive_gap`)을 기록하고 incident와 토스트 알림을 냅니다(ADR-016, ADR-017). 공백 구간은 연구에서 결측으로 처리합니다.
- **해결 계획:** 사용자가 "전원 연결 시 절전 안 함"과 덮개 닫힘 동작을 설정하거나, 절전 방지를 승인합니다(OS 설정 변경이므로 별도 승인).
- **완료 판정 기준:** 설정 후 7일 동안 receive_gap 중 절전 이벤트(506/507)와 겹치는 구간이 0건입니다.
- **Phase 2 영향:** Phase 2는 벌크 과거 데이터(klines, funding)로 동작하므로 막히지 않습니다. 실시간 데이터로 스프레드를 보정하는 일만 표본이 줄어듭니다.

### OPS-002 SSD 온도 감시 작업 등록
- **상태:** BLOCKED(사용자 승인). 관리자 권한(UAC)이 필요합니다. 스크립트 `tools/register_ssd_monitor_task.ps1`와 `tools/ssd_temp_monitor.ps1`(읽기 전용)은 준비돼 있습니다.
- **영향 범위:** 고부하 작업(컴팩션, 백업, 벌크, 30일 지표 재수집)의 온도 차단이 동작하지 않습니다. 상태 파일이 없으면 허용으로 처리합니다(thermal.py).
- **위험도:** 중간~높음. 이 SSD는 유휴 시 80°C, BSOD 이력이 있습니다(메모리 ssd-overheat-bsod).
- **임시 대응책:** 모니터 incident `ssd_monitor`(warn)가 계속 열려 있습니다. 벌크는 한 번에 파일 하나만 받고, BelowNormal 우선순위로 돌립니다.
- **해결 계획:** 사용자가 관리자 권한으로 등록 스크립트를 실행합니다.
- **완료 판정 기준:** `run/ssd_state.json`이 30분 안에 갱신되고, `ssd_monitor` incident가 닫힙니다.
- **Phase 2 영향:** Phase 2 연구 작업도 고부하이므로 같은 thermal 게이트를 적용합니다. 등록 전에는 온도 보호가 없습니다.

### OPS-003 작업 스케줄러 등록과 재부팅 후 자동 복구 검증
- **결정(2026-11-10):** 등록을 진행합니다. 등록 명령은 사용자가 실행합니다.
- **사전 확인(2026-11-10 01:3xZ)**

  | 작업 | 동시 실행 제한 | 자원 영향 | 확인 |
  |---|---|---|---|
  | `qr-upbit-backfill` | 작업 `IgnoreNew`, 잠금 `run/upbit-backfill.lock`(두 번째 실행은 exit 2) | 초당 6회 이하(candle 그룹 한도 10회/IP), 페이지마다 자원·온도 게이트, Upbit 418이면 즉시 exit 5. 수집기는 candle 그룹을 쓰지 않습니다. 파일럿 실측 메모리 76MB | `-WhatIf` 정상, PSScriptAnalyzer 경고 0 |
  | `qr-bulk-extras` | 작업 `IgnoreNew`, klines 다운로드와 같은 잠금 `run/bulk.lock`(klines가 끝날 때까지 exit 2로 대기) | 한 번에 파일 하나, 자원·온도 게이트. 파일럿 bookDepth 월 묶음의 메모리 530MB | `-WhatIf` 정상 |

  - 두 작업 모두 일반 사용자 권한, 우선순위 7(BelowNormal)입니다.
  - 수집기 supervisor와 잠금이 겹치지 않습니다(`supervisor.lock`, `rest.lock`, `ws.lock`과 별개).
  - 2026-11-10 01:3xZ 확인 시 klines 벌크는 마지막 실행 주기의 재검사 단계였습니다(632개 중 66번째 심볼, 기존 파일은 건너뜀).
- **상태:** BLOCKED(사용자 실행 대기)
  - `qr-upbit-backfill`, `qr-bulk-extras` 등록은 자동 모드 분류기가 거부했습니다(2회).
  - 재부팅 검증은 사용자 승인이 필요합니다.
  - `qr-collector-supervisor`와 `qr-bulk-download`는 등록돼 가동 중입니다.
- **영향 범위:** Upbit 분봉 과거 데이터와 T1 추가 데이터(funding, metrics, bookDepth, 현물 klines)가 쌓이지 않습니다. 재부팅 후 수집이 자동으로 재개되는지 확인되지 않았습니다.
- **위험도:** 중간
- **임시 대응책:** 강제 종료 후 복구는 실측했습니다(16.2초). 로그온 트리거 작업은 등록돼 있습니다.
- **해결 계획**
  - 사용자가 실행합니다:
    - `! powershell -NoProfile -File C:\dev\auto\code\tools\register_upbit_backfill_task.ps1`
    - `! powershell -NoProfile -File C:\dev\auto\code\tools\register_bulk_extras_task.ps1`
  - 사용자가 정한 시점에 재부팅하고, 재시작 gap과 하트비트로 복구 시간을 잽니다.
- **완료 판정 기준:**
  - 두 작업이 Ready/Running 상태입니다.
  - 재부팅 후 5분 안에 rest와 ws 하트비트가 재개되고, restart gap이 기록됩니다(10단계 완료 조건).
- **Phase 2 영향:** funding·basis 계열 전략은 bulk funding 이력이 필요합니다. 등록 전에는 파일럿 2개월치만 있어 이 계열의 실데이터 검증이 제한됩니다. Upbit 전략의 실데이터 검증도 마찬가지입니다.

### OPS-004 수집기 프로세스가 살아 있으나 수신이 멈춘 경우(hang)의 감지·복구
- **상태:** OPEN
- **영향 범위:** rest 작업 프로세스가 멈추면(이벤트 루프 정지, 무한 대기) 하트비트가 끊깁니다. 그런데 이 프로세스를 감시하는 모니터가 같은 rest 프로세스 안에 있어서 감지하지 못합니다. supervisor는 종료 코드만 보므로 재시작하지 않습니다. ws 프로세스가 멈춘 경우는 rest 쪽 모니터가 하트비트 120초 지연으로 감지하지만, 알림만 하고 복구는 하지 않습니다.
- **위험도:** 중간. 아직 발생 사례는 없고 가능성만 있습니다. 발생하면 실시간 데이터를 영구히 잃습니다.
- **임시 대응책:** 1초 소스는 수신 공백 스캔이 사후에 gap을 기록합니다(그 스캔도 rest 안에서 돌기 때문에, rest가 멈추면 재시작 뒤에 기록됩니다).
- **해결 계획:** supervisor가 자식의 하트비트(`run/*.heartbeat.json`의 at_ns)를 감시하게 합니다. N초(초깃값 180초) 동안 갱신이 없으면 종료하고 재시작한 뒤, `hang` 원인 gap을 기록합니다. 먼저 테스트를 만들고, 수집기 재시작으로 적용합니다(Phase 1 운영 개선, 별도 작은 계획).
- **완료 판정 기준:**
  - 단위 테스트: 하트비트가 멈춘 가짜 자식을 재시작하는지 확인합니다.
  - 실측: 테스트용 SIGSTOP 대체 수단(예: 디버그 대기 플래그)으로 자식을 멈추고, 180초 + 재시작 대기 안에 복구되는지 확인합니다.
- **Phase 2 영향:** 직접 영향은 없습니다(Phase 2는 과거 데이터 사용).

### OPS-005 외부 백업 부재
- **상태:** OPEN(보류, 2026-11-09 사용자 결정). OneDrive 5GB로는 부족합니다.
- **영향 범위:** SSD 고장, 랜섬웨어, 실수로 삭제하면 실시간 수집분(재수집 불가)과 카탈로그를 잃습니다. 벌크는 다시 받을 수 있습니다.
- **위험도:** 높음
- **임시 대응책:** 같은 SSD의 `C:\dev\qr-backup`에 원본을 증분 백업하고, 카탈로그 스냅샷을 7일 보관합니다. 복원 리허설은 통과했습니다.
- **해결 계획:** 사용자가 외장 디스크나 더 큰 클라우드를 정하면 `QR_BACKUP_ROOT`만 바꿉니다(ADR-008).
- **완료 판정 기준:** 외부 대상에 백업하고, 그 대상에서 복원 리허설이 통과합니다(`tools/restore_drill.py`).
- **Phase 2 영향:** 없습니다. 연구 산출물(`research.db`)도 같은 위험에 포함됩니다.

### OPS-006 벌크 다운로드 미완료
- **상태:** IN_PROGRESS
  - klines 4종: 444/632 심볼(70%), 2026-11-09 23:30Z 기준 약 4.2시간 남음. 실패 3건은 재시도 대상입니다.
  - T1 추가 데이터와 Upbit 분봉: 작업이 등록되지 않았습니다(OPS-003).
- **영향 범위:** 아직 받지 않은 심볼·데이터셋으로는 연구할 수 없습니다.
- **위험도:** 낮음(시간 문제)
- **임시 대응책:** Phase 2는 이미 받은 심볼과 BTCUSDT 등 파일럿 데이터로 개발·검증합니다. 데이터가 없는 조합은 결과를 만들지 않고 "데이터 없음"으로 기록합니다.
- **해결 계획:** 진행 상황을 확인하고, 다 받으면 `bulk/complete-*.json` 완료 표시와 실패 0건을 확인합니다.
- **완료 판정 기준:**
  - `complete-universe-20261109.json`과 `complete-extras-universe-20261109.json`이 있습니다.
  - Upbit 백필 완료 표시가 있습니다.
  - 실패 파일이 0건입니다.
- **Phase 2 영향:** 전체 유니버스 백테스트(생존 편향 포함)는 완료 이후에만 의미가 있습니다. 엔진과 기준 사례 검증은 영향을 받지 않습니다.

### OPS-007 7일 연속 운영 검증 미완료
- **상태:** IN_PROGRESS. 중간 보고는 1.16일치이며, `docs/measurements/validate-week.md`에 있습니다.
- **영향 범위:** 데이터 카탈로그 v1을 보고하지 못했습니다. 실시간 데이터의 품질 수치가 확정되지 않았습니다.
- **위험도:** 중간
- **임시 대응책:** 실시간 데이터에 기반한 비용 보정(스프레드 실측)은 "미검증 데이터" 표시를 달고 보고용으로만 씁니다.
- **해결 계획:** 2026-11-15 이후 `tools/validate_week.py`로 최종 판정합니다. OPS-001(절전)이 해결되지 않으면 커버리지 기준에 미달할 수 있으며, 이 경우에도 결과를 그대로 보고합니다.
- **완료 판정 기준:**
  - 창이 7일 이상입니다.
  - 기록된 gap의 원인이 모두 분류돼 있습니다.
  - OI·비율 재수집 대조, 벌크 체크섬 100%, REST 대조를 보고합니다.
  - 사용자가 커버리지 결과를 확인합니다.
- **Phase 2 영향:** 실시간 데이터(bookTicker 스프레드 등)를 비용 모델에 쓰는 기능은 "미검증" 표시를 단 채로만 동작합니다.

### OPS-008 Claude Code CLI 구독 플랜 표시 불일치 (2026-11-10)
- **상태:** OPEN
- **증상:** `claude auth status --json`(CLI 2.1.321)이 `subscriptionType=pro`, `planTier=pro`를 보고합니다. 사용자는 계정이 Max라고 확인했습니다.
  - 로그인 방식은 `claude.ai`이고 API 제공자는 firstParty입니다. 인증 정보는 출력하거나 기록하지 않았습니다.
- **영향 범위:** Director 실제 LLM 호출(`--llm claude-cli`)의 사용량 한도를 예상할 수 없습니다.
  - Pro와 Max는 5시간 세션 한도와 주간 한도가 다릅니다.
  - API 청구 경로는 쓰지 않으므로 과금에는 영향이 없습니다.
- **위험도:** 낮음. 한도에 걸리면 Director는 재시도하지 않고 PAUSED로 멈춥니다.
- **원인:** **미확인**입니다. 로그인할 때 저장된 플랜 값이 업그레이드 뒤에 갱신되지 않았을 가능성과, 다른 계정이나 조직으로 로그인했을 가능성이 있습니다.
- **해결 계획:** 사용자가 `claude auth logout`, `claude auth login`으로 다시 로그인한 뒤 `claude auth status`로 확인합니다.
  - 인증 토큰은 읽거나 추출하지 않습니다.
- **완료 판정 기준:** CLI가 보고하는 플랜이 사용자 계정 플랜과 같습니다. 그렇지 않으면 공식 지원 채널에 문의한 결과를 기록합니다.

### OPS-009 WS 수집기 7일 크래시 루프를 모니터가 1회만 알림 (2026-11-19, F-017)
- **상태:** 해결(2026-11-20, v2.2 A10): 6시간 재알림, 연속 재시작 incident, paper 등록 표시
- **증상:** 2026-11-12 09:42Z `heartbeat:ws` critical incident가 열리고 토스트 1회 뒤 재경보가 없어 7일간 아무도 몰랐습니다. supervisor는 5분마다 재시작했고 ws는 빈 heartbeat 파일 때문에 시작 직후 죽었습니다.
- **해결 계획:** (1) 열린 critical incident는 N시간(초깃값 6시간)마다 다시 알린다(`monitor/incidents.py`), (2) supervisor가 같은 자식의 연속 재시작 횟수(초깃값 10회)를 넘으면 별도 incident를 연다. OPS-004(hang 감지)와 함께 작은 계획으로 구현합니다.
- **회피책(지금):** 수집기 재배포 뒤 `C:\dev\qr-data\run\*.heartbeat.json`의 갱신 시각과 `logs\supervisor.log`의 `child_exited` 빈도를 하루 1회 눈으로 확인합니다.

## 14. Phase 4 탐색 배치 (2026-11-19)
- 실행: `uv run python tools/run_search.py --s1-count 200 --top-k 10 --promote-rate 0.05 --searchers tpe,random --seed 7 --run-id <id>` (저장소 루트, 작업자 1, BelowNormal, `POLARS_MAX_THREADS=2`). 수집기 rest heartbeat가 120초보다 오래되면 배치를 거부합니다. research.lock을 잡으므로 Director 실행과 동시에 돌리지 않습니다.
- 산출: `docs/measurements/phase4-search-batch.md`(비용 예측/실측 비율, 회계, N_eff·DSR, 순위상관, Random 대 TPE). trial은 운영 research.db에 기록되며 fold `<run-id>:<family>:<searcher>`로 구분합니다.
- 주의: 합성 비교(`--compare-budget-s`)는 TPE ask 비용 때문에 예산보다 훨씬 오래 걸릴 수 있습니다(20초 예산이 30분 이상). 기본값 3초, 0이면 생략.

## 15. Phase 5 validator와 귀무 파이프라인 (2026-11-19)
- validator.db(`C:\dev\qr-data\meta\validator.db`)는 validator 프로세스만 씁니다. 아직 상시 프로세스는 없고 라이브러리·도구만 있습니다. research는 `validation_request`(research.db)에 제출하고 `ack`를 읽기 전용으로 폴링합니다.
- 귀무 검증: `uv run python tools/run_null_pipeline.py --reps 100` → `docs/measurements/phase5-null-pipeline.md`. 반복마다 임시 폴더의 DB를 쓰고 지우므로 운영 DB에 trial이 남지 않습니다. research.lock을 잡지 않지만 CPU를 오래 쓰므로 탐색 배치와 동시에 돌리지 않습니다.

## 16. Phase 6 모의매매 프로세스 (2026-11-19)
- 등록(**사용자 실행**, 자동 모드 분류기가 거부): `! powershell -NoProfile -File C:/dev/auto/code/tools/register_paper_task.ps1` (Git Bash의 `!`는 백슬래시를 지우므로 슬래시로 적습니다). 작업 `qr-paper`, 10분 간격 tick, 로그 `C:\dev\qr-data\logs\paper.log`, heartbeat `run\paper.heartbeat.json`, 잠금 `run\paper.lock`.
- 정지: `C:\dev\qr-data\run\STOP` 파일(수집기와 공유). 수집기 rest heartbeat가 120초보다 오래되면 그 tick은 건너뜁니다.
- 정책: `C:\dev\qr-data\paper\policy.json`(`risk.policy.policy_bytes` 형식) + `policy.sig`(hex) + `policy.key`(HMAC 비밀, 모의매매 전용). 정책이 없거나 만료되면 모든 주문을 거부하고 tick 요약에 `policy` 사유를 남깁니다.
- 대상 전략: validator.db에서 verdict 통과 + 수명주기 `Validated`/`Paper`인 spec만. 현재 그런 전략은 **0개**이므로 프로세스는 heartbeat만 쓰며 주문을 내지 않습니다.
- 30일 운영 판정은 `ledger.db`의 `verify_chain`·`reconcile`과 `paper/divergence.py`의 분해 보고로 하며, 시작 시점은 사용자가 작업을 등록한 날입니다.

## 17. 모의매매 작업 해제 (v2.2 A10, 2026-11-20)
- `tools/register_paper_task.ps1`은 등록 뒤 `C:\dev\qr-data\run\paper.expected`를 만든다. 이 표시가 있으면 모니터는 paper heartbeat가 없어도 `heartbeat:paper` critical을 열고 6시간마다 다시 알린다.
- **해제 절차:** `Unregister-ScheduledTask -TaskName qr-paper -Confirm:$false` → `C:\dev\qr-data\run\paper.expected`와 `paper.heartbeat.json` 삭제. 표시만 남기면 경보가 계속된다.
- supervisor 상태(`run/supervisor.state.json`)는 60초마다 다시 쓰이며, 10분보다 오래된 상태는 모니터가 무시한다(supervisor가 꺼진 동안 이전 연속 재시작 횟수로 경보하지 않음).

## 18. 멈추지 않는 자율 캠페인 예약 작업 (v2.3, ADR-049, 2026-11-22)
- 예약 작업 등록은 자동 모드에서 거부되므로 사용자가 직접 실행한다(Gotchas). 세 작업 모두 사용자 권한(관리자 아님), 우선순위 7(BelowNormal), IgnoreNew로 등록된다. 각 스크립트는 `-WhatIf`로 무엇을 등록할지 먼저 보여 준다. 인자 검증 테스트: `Invoke-Pester -Path tests/powershell`.
- 등록 명령(Claude Code 프롬프트에서 `!`로 시작해 실행):
  - `! powershell -NoProfile -File C:/dev/auto/code/tools/register_campaign_task.ps1`
    - 매시간 실행한다. 한 번에 최대 6시간, 30일 동안 돈다. c1을 사전 등록 격자로 이어서 돌리고, 라운드마다 G2~G4 묶음과 재개방 셀을 연다.
    - 틱이 끝날 때마다 후보를 validator 대기열(outbox)에 넣는다.
    - 로그: `qr-data/logs/campaign-c1.log`
  - `! powershell -NoProfile -File C:/dev/auto/code/tools/register_data_queue_task.ps1`
    - 2시간마다 실행하고, 한 번에 tier 하나의 한 단계만 처리한다(파일럿 → 투영 → 상한 검사 → 전체).
    - 로그: `qr-data/logs/data-queue.log`, 상태: `qr-data/run/data-queue-state.json`
  - `! powershell -NoProfile -File C:/dev/auto/code/tools/register_validator_task.ps1`
    - 매시간 실행해 대기 중인 요청을 holdout에서 판정한다. 결과는 1비트로만 남긴다.
    - 로그: `qr-data/logs/validator.log`. 트레이스백에 지역변수가 남지 않는다(holdout 값 차단).
- 멈추기:
  - `Disable-ScheduledTask`는 다음 틱만 막는다. 실행 중인 틱(캠페인 최대 6시간)은 계속 돈다.
  - 캠페인을 지금 멈추려면 `qr-data/run/STOP-campaign` 파일을 만든다(다음 배치 경계에서 멈춤). 그리고 `Disable-ScheduledTask qr-campaign`.
  - 곧바로 끊으려면 `Stop-ScheduledTask`. 끊긴 배치는 재개할 때 같은 trial로 다시 돈다.
  - 데이터 큐·validator: `Disable-ScheduledTask` 또는 `Stop-ScheduledTask`에 `qr-data-queue` / `qr-validator`.
- 다시 시작하기:
  - `STOP-campaign` 파일을 지우고 `Enable-ScheduledTask`.
  - 세 작업 모두 상태 파일·catalog·ledger에서 이어 간다.
- 상태 확인:
  - 캠페인 라운드와 원인: `qr-data/run/campaign-c1.md`
    - 예약 실행은 저장소 밖에 쓴다. 커밋된 측정표를 매 틱 덮어쓰지 않기 위해서다.
    - 기록으로 남길 때 `docs/measurements/`로 복사해 커밋한다.
  - 캠페인이 기다리는 데이터: `qr-data/run/campaign-c1.json`의 `data_requests`와 캠페인 로그의 `waiting_data` 사유.
    - `data-queue.json`은 큐 순서(요청 + 기본 tier 6개 전부)이지 대기 목록이 아니다.
  - tier 상태: `qr-data/run/data-queue-state.json`
  - `skipped_cap` tier는 lake 상한(200GB)이나 디스크 여유 때문에 멈춘 것이다. 상한 상향은 사용자 결정이다.
- 동시 실행:
  - 연구 프로세스는 `run/research.lock`으로 하나만 돈다. 캠페인 두 개나 run_search와는 같이 돌지 않는다.
  - 다운로드:
    - tier 전체 다운로드는 다른 벌크 다운로드와 `run/bulk.lock`을 공유한다. 잠금이 잡혀 있으면 다음 틱까지 기다린다.
    - 파일럿은 별도 잠금(`bulk-extras-pilot.lock`)을 쓰므로 다른 다운로드와 같이 돌 수 있다.
    - 외부 소스(`fetch_external.py`)는 잠금이 없다. 예약 작업의 IgnoreNew로만 한 번에 하나씩 돈다.
  - validator는 `run/validator.lock`으로 하나만 돈다.
  - 캠페인과 다운로드는 같이 돌 수 있다. 메모리 가드는 research gate 2GB, 다운로드 RAM 1.5GB이다.
- tier 상태(`data-queue-state.json`)의 뜻:
  - `pending`·`piloted`·`running`: 진행 중. 이 tier의 데이터셋은 캠페인에서 숨겨진다(끝난 tier만 쓴다, ADR-049 보강 2).
  - `deferred`: 진척 없는 종료나 오류가 이어져 다른 tier 뒤로 미뤄졌다. 계속 재시도한다(오프라인이면 여기로 온다).
  - `done`: 완료. 이때부터 그 데이터를 요구하는 셀이 다시 열린다.
  - `skipped_cap`: lake 상한이나 디스크 여유 때문에 멈췄다. 사용자 결정이 필요하다. 다시 시도하려면 그 항목을 상태 파일에서 지운다.
  - 알림은 없다. 상태 파일과 `data-queue.progress.json`(마지막 틱)을 직접 확인한다.
- 캠페인 틱의 종료 코드: 0(라운드 대기·예산), 1(tier 상태 파일이나 다른 캠페인 파일을 읽을 수 없음), 2(다른 연구 프로세스가 잠금 보유), 3(자원 가드·STOP 파일).
- validator 질의 예산(k = 20)은 자동 제출된 후보가 판정될 때마다 줄어든다. 사전 조건 미달로 거부된 요청은 예산을 쓰지 않는다.
- 세션에서 손으로 돌리는 `run_data_queue.py`와 예약 작업은 `run/data-queue.lock`으로 겹치지 않는다(늦게 온 쪽이 exit 2).
- 첫 등록 뒤 첫 캠페인 틱은 패널 캐시를 다시 만든다(패널 코드 변경, 창·주기·top-M 조합마다 약 8~10분).
