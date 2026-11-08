# 운영 문서 (T0 원본 수집)

## 1. 상시 실행
| 항목 | 내용 |
|---|---|
| 실행 주체 | Windows 작업 스케줄러 `qr-collector-supervisor`. 일반 사용자(`LogonType Interactive`, `RunLevel Limited`), 저장된 비밀번호 없음 |
| 등록·갱신 | `powershell -File C:\dev\quant-research\tools\register_collector_task.ps1`. 다시 실행해도 안전하며, 실행 중인 인스턴스는 유지됩니다 |
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

## 6. 미결 항목
- 외부 백업(OneDrive) 검증과 구성
- SSD 온도 감시 작업(관리자 예약 작업, 읽기 전용)
- 재부팅 후 재개 검증
