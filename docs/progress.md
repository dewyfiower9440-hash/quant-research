# 진행 현황 (Phase 0~1)

> 기준: `docs/plans/2026-11-08-phase0-1-build.md`(실행 계획, 승인 2026-11-08) / 설계 v2.1
> 상태 값: TODO / IN_PROGRESS / DONE / PARTIAL / BLOCKED. DONE은 완료 조건, 선언 게이트, CI를 모두 통과했을 때만 씁니다.
> 재개 방법: 이 파일에서 첫 번째로 DONE이 아닌 작업을 찾고, 실행 계획의 같은 단계 번호를 함께 읽습니다.

## Phase 1 상태: **IMPLEMENTED / OPERATIONAL_VALIDATION_PENDING** (2026-11-09 사용자 결정)
- **의미:** 핵심 기능 구현과 단위·통합 검증(선언 게이트, CI)은 인정합니다. 운영 검증은 끝나지 않았습니다. 아래 미완료 항목은 PASS로 처리하지 않고, 운영 문서 §11의 이월 이슈(OPS-001~007)로 추적합니다.
- **구현·검증 완료**(증거: 이 파일의 각 절, `docs/test-results.md`, CI run id)
  - 원본 캡처(T0)
  - 카탈로그 스키마, 상품·유니버스
  - as_of / join_asof와 봉인
  - 컴팩션·정규화
  - 벌크 klines 다운로더와 REST 대조 100/100
  - T1 추가 벌크 다운로더(파일럿)
  - Upbit 분봉 백필(파일럿)
  - 모니터·incident·알림
  - 강제 종료 후 복구 16.2초
  - 원본·카탈로그 복원 리허설
  - 7일 검증 도구
- **운영 검증 미완료**(PASS 아님)

  | 이슈 | 내용 |
  |---|---|
  | OPS-001 | 절전으로 실시간 데이터 공백 |
  | OPS-002 | SSD 온도 감시 미등록 |
  | OPS-003 | 작업 등록 2건과 재부팅 후 복구 미검증 |
  | OPS-004 | 프로세스는 살아 있는데 수신이 멈춘 경우의 감지·복구 |
  | OPS-005 | 외부 백업 없음 |
  | OPS-006 | 벌크 다운로드 미완료(klines 진행 중, T1 추가 데이터와 Upbit 분봉은 작업 미등록) |
  | OPS-007 | 7일 검증 미완료 |

- **실행 계획 체크박스:** `2026-11-08-phase0-1-build.md`의 4·8·9·10·11단계는 운영 검증이 끝날 때까지 체크하지 않습니다.

| # | 작업 | 상태 | 마지막 갱신 |
|---|---|---|---|
| 1 | Scaffold와 저장소 | DONE | 2026-11-08 |
| 2 | Phase 0 측정 | DONE (CI run 42469337357) | 2026-11-08 |
| 3 | 원본 캡처기 최소판 | DONE (CI run 42480240871) | 2026-11-08 |
| 4 | 조기 백업과 복원 리허설 | PARTIAL (원본·카탈로그 리허설 일치. 외부 대상 미정) | 2026-11-09 |
| 5 | 카탈로그 스키마 | DONE (CI run 42531815458) | 2026-11-09 |
| 6 | as_of / join_asof API | DONE (CI run 42536249442, F-007로 재확정) | 2026-11-09 |
| 7 | 컴팩션과 정규화 | DONE (CI run 42536249442) | 2026-11-09 |
| 8 | Binance 벌크 T1 | IN_PROGRESS (klines 4종 418/632 심볼, 68%, 실패 3(재시도 대상). REST 대조 100/100 일치. funding·metrics·bookDepth·현물 klines 구현·파일럿 완료, 전체 실행 작업 등록 대기) | 2026-11-09 |
| 9 | Upbit 분봉 백필 | IN_PROGRESS (구현·파일럿·trades 대조 완료. 전체 실행 작업 등록이 자동 모드에서 거부됨: 사용자 실행 필요) | 2026-11-09 |
| 10 | 모니터와 작업 스케줄러 | PARTIAL (모니터·incident·토스트 알림, 강제 종료 복구 16.2초 실측 완료. 재부팅 후 복구 검증, SSD 감시 작업 등록 남음: 사용자 승인 필요) | 2026-11-09 |
| 11 | 7일 연속 운영 검증 | IN_PROGRESS (도구 완료, 중간 보고 1.16일. 최종 판정 2026-11-15 이후) | 2026-11-09 |

---

## 1. Scaffold와 저장소
- **완료 조건:** 게이트 통과, CI 성공(run id 기록).
- **구현 파일**
  - scaffold 산출물: `pyproject.toml`, `uv.lock`, `src/quant_research/*`, `tests/*`, `CLAUDE.md`
  - `.github/workflows/ci.yml`: Actions SHA 고정, pip-audit 단계 추가
  - `docs/*`
- **실행 명령**
  - `python -I C:/dev/tools/scaffold/new_project.py quant-research --stack python`(`C:\dev`에서 실행)
  - 게이트: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q --cov`
- **진행 메모:** scaffold는 빈 폴더를 요구해서 `docs/`를 `C:\dev\qr-docs-staging`으로 옮겼다가 되돌렸습니다. 작업 폴더를 지우는 `rmdir`은 안전 검사가 막았고 필요하지도 않았습니다(scaffold는 빈 폴더를 허용).
- **검증 결과:** 로컬 게이트 통과(ruff, format, pyright 0 errors, pytest 14 passed, 커버리지 93.06%). GitHub private 저장소 `dewyfiower9440-hash/quant-research`를 생성했습니다. **CI Verified: run 42468357091**(main, push, 커밋 923b1fe, job `check` 성공, pip-audit 포함).
- **미해결 문제:** Dependabot이 Actions 갱신 PR 2개(checkout 7.0.1, setup-uv 10.3.3)를 열었습니다. 의존성 갱신은 승인 범위 밖이라 병합하지 않았습니다. 사용자 결정이 필요합니다.
- **다음 작업:** 2. Phase 0 측정

## 2. Phase 0 측정
- **완료 조건:** 측정표 작성, bookTicker 수집 방식 결정을 `decisions.md`에 기록.
- **구현 파일**
  - `src/quant_research/domain/probe.py`(순수 함수: 시계 offset, 메시지율, epoch 단위, 단조 이진 탐색, 격자 정렬)
  - `tests/test_probe.py`(17개, hypothesis 포함)
  - `tools/phase0_probe.py`(얇은 I/O 셸, CI·커버리지 대상 아님)
  - `docs/measurements/phase0.md`, `decisions.md` ADR-003~005
- **실행 명령:** `uv run python tools/phase0_probe.py --ws-seconds 30` + REST bookTicker 30회(측정표 하단)
- **검증 결과**
  - 게이트 통과: 31 passed, 커버리지 95.87%, pyright 0 errors
  - 실측 결과 요약
    - 선물 WS 접속 **가능**(이전의 차단 기록은 현재 해당 없음)
    - bookTicker는 REST 1초로 결정(ADR-003)
    - Binance 시계 offset −16ms
    - 벌크 metrics에 OI와 비율 이력이 있음(5분 격자 결측 발견)
    - Upbit 1분봉은 2017-09-25까지 조회됨
    - `ask_bid` BID = 매수 주도(부분 확인)
- **변경 사항**
  - `pyproject.toml`에 ruff isort `known-first-party`를 추가했습니다. 편집기 포맷 hook과 게이트가 import 그룹을 서로 다르게 판단해 충돌했기 때문입니다(Gotcha로 기록).
  - 의존성 추가: httpx, websockets, polars, pyarrow, duckdb, numba
- **실패 이력:** F-002(Polars 컬럼명 중복, 해결)
- **미해결 문제**
  - SSD SMART 온도 미측정(관리자 권한 승인 필요)
  - `ask_bid`의 ASK 방향 미확인
  - metrics 시각이 구간 시작인지 끝인지 미확인(보수적으로 가정)
  - 저장량은 30초 외삽값이라 작업 3에서 1시간 실측
- **다음 작업:** 3. 원본 캡처기 최소판

## 3. 원본 캡처기 최소판 (DONE)
- **완료 조건(실행 계획)**
  - fixture 테스트로 파서와 레이트 리미터 검증
  - 강제 종료 후 꼬리 격리와 gap 등록
  - 2중 실행 차단
  - 실제 1시간 연속 수집과 결과 확인
- **구현 파일:** `src/quant_research/capture/`
  - `segment.py`: JSONL 세그먼트, 형식 v2, fsync, 오류 시 파일 포기
  - `recovery.py`: 꼬리 격리와 gap 기록
  - `lock.py`: 단일 인스턴스 잠금
  - `ratelimit.py`: 거래소별 418/451/429 처리
  - `clockref.py`: 서버 시각 offset
  - `http.py`: 재시도와 공유 VenueGate
  - `sinks.py`
  - `binance.py`, `upbit.py`: T0 수집원
  - `run.py`: rest/ws/supervisor, STOP 파일, heartbeat
  - 테스트: `tests/capture/` 5개 파일(97개 테스트 중 capture 66개)
- **실행 명령**
  - 상시 수집: `uv run python -m quant_research.capture.run supervisor`(`C:\dev\quant-research`)
  - 정상 종료: `C:\dev\qr-data\run\STOP` 파일 생성
- **T0 수집원(실제 동작 확인)**
  - Binance: bookTicker 1초(상위 50, REST), server_time, exchangeInfo·24hr(spot/um), OI·롱숏·taker·basis 30일 재수집(6시간 주기), forceOrder WS
  - Upbit: 호가 L1 1초(상위 20), 체결 cursor 따라잡기(30분 주기), 마켓·경보 스냅샷
  - **공지 WS는 제외**했습니다. 한국 API가 `announcement` 타입을 `INVALID_PARAM`으로 거부합니다(2026-11-08 실측).
- **검증 결과(지금까지)**
  - 게이트 통과: 97 passed, 91.49%
  - CI: run 42471093283(aaba1ac), 42471562536(19e067c, F-004), 42472784613(d57e9df, F-005)
  - 1시간 실측은 수정 전 코드(aaba1ac)로 진행 중입니다.
- **실패 이력:** F-003(테스트 오류), F-004(ws heartbeat), F-005(리뷰 11건). 모두 해결했습니다.
- **미해결 문제**
  - 1시간 실측 결과 분석
  - 수정된 코드로 상시 수집 재시작
  - 세션이 끝나도 수집이 계속되도록 작업 스케줄러 등록(작업 10의 일부를 앞당겨 할지 결정)
- **1시간 실측 결과(aaba1ac, 2026-11-08 10:05~11:05 UTC)**
  - bookTicker 3,597/3,600, Upbit 호가 3,598, forceOrder 2,998, 작업 오류 1건
  - 원본 1.45GB(Upbit 체결 7일 백필 1.1GB 포함)
  - 압축률: gzip-6 8.7~19배 → ADR-006(압축), ADR-007(지표 주기)
- **추가 결함과 수정**
  - F-005(리뷰 11건), ADR-006/007
  - supervisor가 STOP 파일을 무시하던 문제(39e5a94, 수정 전 코드로는 테스트 실패 확인)
- **완료 판정 근거**
  - fixture 테스트, 꼬리 격리, gap 등록, 2중 실행 차단 테스트: 모두 통과(107 passed)
  - 실제 1시간 연속 수집: 확인
  - 상시 수집: 39e5a94로 재가동했고, 두 프로세스의 heartbeat 정상, restart gap 기록 확인
- **운영 상태:** 상시 수집은 **현재 Claude Code 세션의 백그라운드 작업**으로 돌고 있습니다. 세션이 끝나거나 PC가 재부팅되면 멈춥니다. 영구 운영은 작업 10(작업 스케줄러)에서 처리합니다.
- **남은 미확인**
  - `ask_bid`의 ASK 방향
  - metrics 시각의 구간 기준
  - basis 오류 10개 심볼(XAUUSDT, SOXLUSDT 등 TradFi 계열로 추정)이 basis를 제공하지 않는지
- **다음 작업:** 4. 조기 백업과 복원 리허설

## 4. 조기 백업과 복원 리허설 (PARTIAL)
- **완료 조건:** 복원 후 sha256과 행 수 100% 일치. 외부 대상이 없어 "부분 완료"입니다.
- **구현 파일**
  - `src/quant_research/capture/backup.py`(증분 백업, 복원 검증)
  - `config.backup_root`
  - rest 프로세스의 `backup` 작업(1시간 주기)
  - 테스트 `tests/capture/test_backup.py`
- **실행 결과(실데이터, 2026-11-08)**
  - 백업: 72개 파일, 27.2초, 177MB
  - 복원 리허설: 72/72 일치, 29.0초
  - 재실행: 복사 0건, 변경 없음 72건
- **카탈로그 스냅샷 추가(2026-11-09, 마무리 계획 1단계)**
  - 1차 리허설에는 `meta/catalog.db`가 빠져 있었습니다.
  - 이번에 추가한 기능
    - `snapshot_catalog`: `VACUUM INTO`로 스냅샷을 만들고(다른 연결이 쓰는 중에도, WAL 포함 일관), 하루 1개씩 최근 7일을 보관하며, 스냅샷마다 sha256과 테이블별 행 수를 기록합니다.
    - `restore_catalog_and_verify`: sha256, `pragma integrity_check`, 테이블별 행 수를 확인합니다.
    - rest 백업 job이 매시간 실행합니다.
    - `tools/restore_drill.py`
  - 테이블 목록 `ROW_COUNT_SQL`이 스키마와 같은지 테스트로 묶었습니다. 마이그레이션을 추가하면 이 목록도 갱신해야 합니다.
  - **실데이터 리허설(2026-11-09 13:2xZ):** `tools/restore_drill.py --to C:/dev/qr-restore-drill --snapshot` → exit 0
    - 원본 711/711개 일치(120.8초)
    - 카탈로그 스냅샷 1/1개 일치(0.4초): bulk_file 24,308행, gap 6,538행, compacted_segment 337행 등
- **외부 백업 보류(2026-11-09 사용자 결정):** 사용자가 OneDrive 용량을 5GB로 확인했습니다. 현재 데이터 규모(원본, 레이크, 벌크 수십 GB)에 비해 부족하므로 외부 백업 구축은 보류합니다. 같은 SSD에 두는 로컬 백업(`C:\dev\qr-backup`)은 그대로 유지합니다.
- **위험(외부 백업 없음)**
  - SSD 고장, 파일 시스템 손상, 랜섬웨어, 실수로 인한 삭제가 일어나면 원본과 백업을 함께 잃습니다.
  - 특히 실시간 수집분(강제청산 WS, 호가 1초, Upbit 체결)은 거래소에서 다시 받을 수 없어 영구 손실입니다.
  - 벌크 1분봉은 data.binance.vision에서 다시 받을 수 있어 복구 가능합니다.
  - 이 SSD는 과열 이력이 있습니다(메모리 `ssd-overheat-bsod`).
  - 외부 대상(외장 디스크 또는 더 큰 클라우드 용량)이 정해지면 `QR_BACKUP_ROOT` 경로만 바꾸면 됩니다(ADR-008).

## 4b(=10 일부). 작업 스케줄러 상시 실행
- **구현 파일:** `tools/register_collector_task.ps1`(PSScriptAnalyzer 0건), `run.py --log-dir`
- **검증**
  - 세션과 무관한지: 부모 프로세스가 `svchost`(Task Scheduler)임을 확인
  - 중복 차단: exit 2 확인
  - 크래시 복구: 268초 만에 재시작(F-006 수정 후)
  - gap 기록: 정확함
- **미확인:** 재부팅 뒤 로그온했을 때 자동 재개. 사용자가 재부팅하면 `_gaps/process.rest.jsonl`과 heartbeat로 확인합니다.

## 5. 카탈로그 스키마 (DONE)
- **완료 조건:** 마이그레이션 테스트, lag 정책 하향 변경 거부 테스트
- **구현 파일**
  - `src/quant_research/catalog/migrations/0001_initial.sql`
  - `catalog/store.py`(Catalog API)
  - `catalog/bootstrap.py`(T0 데이터셋, lag 사전값, 봉인 epoch 1, gap 가져오기)
  - 테스트 `tests/catalog/test_catalog.py`(10개)
- **불변식(SQL 트리거와 Python 양쪽에서 강제)**
  - `seal_policy`와 `lag_policy` 행은 수정·삭제할 수 없음
  - lag는 늘리는 방향으로만 바꿀 수 있음
  - epoch는 연속 번호이고 경계가 뒤로 가지 않음
  - gap 원인은 정해진 값만 허용하고, 구간은 시작 ≤ 끝
  - 유니버스는 시점별로 관리하며 구간 끝은 포함하지 않음
- **실데이터:** `C:\dev\qr-data\meta\catalog.db` 초기화(데이터셋 16, lag 16, epoch 16, gap 13). 재실행해도 0건 추가(멱등)
- **결정:** ADR-011(T0 봉인 60일/30일 사전 등록)
- **다음 작업:** 6. as_of / join_asof API

## 운영 변경(2026-11-09, 결정 6) 복구 지연 단축
- 작업 스케줄러 반복 간격을 5분에서 **1분**으로 줄였습니다(`-RepeatMinutes`, 기본 1).
- **실측:** 프로세스 트리를 강제 종료한 뒤 **46초** 만에 재시작(이전 268초)
- 단일 인스턴스 보장(`IgnoreNew` + 잠금)과 데이터 경로는 바뀌지 않았습니다.
- 운영 절차와 제약은 `docs/operations.md`에 정리했습니다.

## 6. as_of / join_asof API (DONE)
- **완료 조건:** 직접 읽기 금지 아키텍처 테스트, 누수 테스트(미래 값 주입, 구간 시작 타임스탬프 주입)
- **구현 파일**
  - `src/quant_research/data/access.py`(`PointInTime`: `with_available`, `rows_visible`, `join_asof`, `SealViolationError`)
  - 테스트 `tests/data/test_access.py`(12개)
- **동작**
  - `available_ns`는 읽을 때 계산합니다(시각 의미 + lag 정책 버전).
  - 구간 데이터는 시각 의미가 미확인이면 구간 시작으로 간주합니다(+구간 길이 +lag).
  - 실시간 관측 데이터는 `received_ns`를 씁니다.
  - research 역할: 봉인 경계 이후의 결정 시각을 요청하면 `SealViolationError`, 경계 이후에 가용해지는 행은 제외합니다.
  - validator 역할만 holdout을 읽을 수 있습니다.
  - lag 버전을 고정해 같은 결과를 재현할 수 있습니다.
- **아키텍처 테스트:** `src` 안에서 `pyarrow`/`duckdb` import와 `read_parquet`/`scan_parquet` 호출은 `data/lake.py`에서만 허용합니다(작업 7에서 구현).
- **경고 처리:** Polars가 `by` 그룹에서는 정렬을 검사하지 못한다는 경고를 냈습니다. 코드가 직접 정렬하는 것을 근거로 `by`를 쓸 때만 `check_sortedness`를 끄고, 행 순서를 섞은 입력 테스트로 정확성을 검증했습니다. 경고를 오류로 바꿔 돌린 실행(`-W error::UserWarning`)도 146개 모두 통과했습니다.
- **다음 작업:** 7. 컴팩션과 정규화

**정정(F-007):** 작업 6의 첫 CI(run 42533347966)에는 `src/quant_research/data/`와 `tests/data/`가 빠져 있었습니다. `.gitignore` `data/` 패턴 때문입니다. 따라서 그 시점의 DONE 판정은 잘못이었습니다. 수정 커밋의 CI run 42536249442(169 passed)로 다시 확정했습니다.

## 7. 컴팩션과 정규화 (DONE)
- **완료 조건:** 재생 결정성, 리더가 파일을 연 상태에서 컴팩션 성공, 재수정 시 두 버전 모두 조회 가능
- **구현 파일**
  - `normalize/rows.py`(데이터셋 11개 정규화, 실제 payload 형태 기준)
  - `data/lake.py`(Parquet 입출력을 이 모듈에만 둠, 바이템포럴 리더)
  - `data/compact.py`(세그먼트 하나당 불변 Parquet 하나, catalog `compacted_segment`로 멱등성 보장, CLI)
  - migration `0002_compaction.sql`
  - rest 프로세스의 `compact` 작업(10분 주기, 온도 차단 대상)
- **테스트:** `tests/normalize/test_rows.py`(13), `tests/data/test_compact.py`(10)
- **실데이터(2026-11-09)**
  - 컴팩션: 세그먼트 188개, 21,788,866행, 실패 0, 3분 37초
  - bookTicker 2,219,400행, 52개 심볼, 12.6시간
  - OI 456,285행. 재수정 버전은 아직 0건(ADR-007 가정은 추후 재평가)
- **배포:** 운영 문서의 정지 절차로 수집기를 재배포했습니다(정상 종료 19초, 이후 정상).
- **다음 작업:** 8. Binance 벌크 T1

## 리뷰 수정 (2026-11-09, F-010, 계획 `docs/plans/2026-11-09-review-fixes.md`)
- 전체 리뷰에서 나온 결함을 수정했습니다(조회 계층의 시점 정합, 컴팩션 메모리와 실패 격리, 벌크 카탈로그 원자성). 모두 테스트를 먼저 작성했습니다.
- 커밋: 61bcaff, 1f3e6c0, 2e556c3, 7353810. CI run 42551635899 success, 215 passed.
- 수집기는 02:35Z에 STOP으로 재시작했습니다(공백 rest 19초, ws 7초, gap 기록됨). rest 작업 프로세스 메모리: 717MB → 62MB(재시작 직후) → **66MB**(03:01Z 실제 컴팩션 뒤: 세그먼트 24개, 225만 행, 실패 0). 컴팩션 자식 프로세스의 최대 메모리는 미측정입니다.

## 10a. 수신 공백 감지와 백업 경쟁 수정 (2026-11-09, F-011, ADR-016)
- **계획:** `docs/plans/2026-11-09-step10-gaps-backup.md`
- **구현:** `data/gapscan.py`, migration `0005_gapscan.sql`, `capture/run.py`(컴팩션 자식 프로세스에서 스캔), `capture/backup.py`(사라진 파일 건너뜀)
- **검증**
  - 게이트 235 passed, CI run 42585795643 success
  - 08:27Z 재시작 뒤 첫 스캔에서 소급 gap 20건을 기록했습니다(절전 190.2분, Upbit 장애 174.3분과 50.3분 포함).
- **절전 후 점검(재부팅 아님)**
  - 실시간 3개 소스에 190분 공백이 생겼고, 이는 영구 손실입니다.
  - Upbit 체결(20개 마켓, 5분 넘는 빈 곳 없음)과 Binance 지표는 깨어난 뒤 자동으로 따라잡았습니다.
  - 시계 offset은 53ms에서 70~112ms로 변했고, clock_suspect는 없었습니다.
- **10단계 나머지(이번 범위 밖):** 디스크, RAM, 시계 경보, 로그온 세션 알림, 재부팅 후 재개 검증

## 9. Upbit 분봉 백필 (구현·검증 완료, 전체 실행 대기)
- **세부 계획:** `docs/plans/2026-11-09-step9-upbit-candles.md`, ADR-015
- **구현 파일**
  - `backfill/upbit_candles.py`
  - migration `0004_backfill.sql`
  - `adapters/jobhost.py`(공유 헬퍼)
  - `tools/upbit_candles_backfill.py`, `tools/validate_upbit_candles.py`, `tools/register_upbit_backfill_task.ps1`
- **테스트:** `tests/backfill/`(9개)
  - 페이지 처리, 체결 없음 구간, 중단 후 재개 시 구간 누락·중복 없음, 완료된 마켓 건너뜀
  - 봉이 없는 마켓, 페이지가 과거로 진행하지 않는 API 오류, 원자성, 요청 속도 제한, 겹치는 실행의 중복 제거
- **파일럿(2026-11-09 03:18Z)**
  - KRW-BTC: 30페이지, 6,000봉, no_trade 구간 4개
  - KRW-USDG: 21번 요청해 전체 이력 완료, 3,874봉, no_trade 구간 2,778개(거래가 드문 마켓은 봉 1개당 약 0.7개)
  - 요청 속도 약 6회/초, private 메모리 76MB
- **대조 검증(완료 조건)**
  - T0 `upbit.trades`를 1분 OHLCV로 집계해 KRW-BTC 5,968분을 비교했습니다. 5,967분에서 OHLCV가 정확히 일치했습니다.
  - 어긋난 1분(02:35Z)은 수집기 재시작 시점으로, 그 분의 체결이 아직 컴팩션되지 않은 경계입니다.
  - 2026-11-08 17:00~22:00Z(300분)는 양쪽 모두 데이터가 없어 Upbit 측 거래 중단으로 판단했습니다. 같은 시간대에 실시간 수집기의 ConnectTimeout(17:02~20:48Z)이 있었습니다.
- **남은 일:** 전체 실행(상위 20 마켓)을 작업 스케줄러에 등록해야 합니다. 등록 명령이 자동 모드 권한 분류기에 거부되어, 사용자의 결정을 기다리고 있습니다.

## 8. Binance 벌크 T1 (IN_PROGRESS)
- **완료 조건:** 체크섬 100%, 대조 샘플이 REST와 일치
- **구현 파일**
  - `bulk/universe.py`(시점별 상위 K, 순수 함수)
  - `tools/bulk_universe.py`(유니버스와 정확한 용량 산출)
  - `bulk/klines.py`(CSV 파싱, 1분 격자 QA)
  - `bulk/download.py`(메모리 안에서 체크섬 검증, 불변 Parquet, `bulk_file` 기록, 재생성 버전)
  - migration `0003_bulk.sql`
  - `tools/bulk_download.py`(재개 가능, 온도 차단 적용)
  - catalog bootstrap(ADR-013)
- **테스트:** `tests/bulk/`(18개)
- **결과**
  - 유니버스: 632개 심볼(253초)
  - 파일럿: BTCUSDT 4종 × 3개월, 12개 파일, 519,840행, 12초, 실패 0, gap 4(2026-10 파일 불완전)
- **1차 전체 실행(2026-11-09 09:46~10:02):** 1,053개 파일, 1.06GB, 12/632 심볼, 실패 0. Claude 세션의 백그라운드 작업이 메모리 부족으로 강제 종료돼 중단됐습니다(F-009).
- **재개(2026-11-09 사용자 결정 1번, 작업 스케줄러)**
  - 실행 전 측정: RAM 15.6GB 중 가용 6.5GB, C: 여유 318GB, SSD 온도 상태 파일 없음(감시 작업 미등록)
  - 실행 방식: 작업 `qr-bulk-download`(`tools/register_bulk_task.ps1`)
    - 일반 사용자(Limited), 우선순위 7(BelowNormal 확인)
    - 30분마다 반복(7일 동안), IgnoreNew
  - 보호 장치(`tools/bulk_download.py`, `bulk/guard.py`)
    - 동시 실행 1(파일 단위 순차 처리), `POLARS_MAX_THREADS=2`
    - 단일 인스턴스 잠금 `run/bulk.lock`
    - 파일마다 확인: SSD 온도, 디스크 여유 50GB 이상, 벌크 상한 200GB(80%에서 경고), 가용 RAM 1.5GB 이상, 자기 프로세스 private 메모리 1GB 이하. 위반하면 exit 3으로 멈추고 다음 실행에서 재개합니다.
    - 진행 상태 `run/bulk.progress.json`, 로그 `logs/bulk-download-task.log`
    - 전체 완료이고 실패가 0이면 `bulk/complete-universe-20261109.json`을 기록하고, 이후 실행은 바로 종료합니다.
  - 검증
    - 2중 실행 차단: 잠금을 쥔 상태에서 실행하면 exit 2
    - 소규모 재개 테스트(0GUSDT, 1000XECUSDT): 이미 받은 41개는 건너뛰고 91개를 새로 받음(390만 행, 73.6MB), 실패 0, gap 18, exit 0. 최대 working set 195MB, private 254MB
    - 작업 시작 10:42:25: 앞의 11개 심볼(1,132개 파일)을 건너뛰고 이어서 진행
- **남은 일:** 전체 완료 후 체크섬 실패 0 확인, REST 대조 샘플 검증, 용량 실측

## Phase 1 마무리 (2026-11-09, 계획 `docs/plans/2026-11-09-phase1-completion.md`)
- **시작 시점 점검(13:06Z)**
  - 벌크 klines 345/632(57.7%), 실패 3건: MATICUSDT premium 2021-06~08. 절전에서 깨어난 직후 DNS 실패로 생겼으며, 다음 실행에서 재시도됩니다.
  - 수집기는 정상이었고 basis 400 소음이 23개 심볼에서 났습니다.
  - **두 번째 절전** 11:39:39Z~13:02:55Z(83분)를 발견했습니다.
- **1. 카탈로그 백업(4단계):** `snapshot_catalog`/`restore_catalog_and_verify`. 실데이터 리허설 결과 원본 711/711개, 카탈로그 1/1개가 일치했습니다(§4).
- **2. 모니터(10단계)**
  - 구성: `monitor/checks.py`(순수 판정), `monitor/incidents.py`(카탈로그 `incident`, 마이그레이션 0006), `monitor/observe.py`(rest job, 5분 주기), `adapters/notify.py` + `tools/notify.ps1`(로그온 세션 토스트), `run/alerts.log`
  - 점검 항목: ws 하트비트 120초, 디스크 50GB/20GB, RAM 1GB, 시계 250ms·5분, SSD 상태, 5분 이상 수신 공백
  - basis -4104 심볼은 처음 한 번만 보고하고 이후에는 건너뜁니다.
  - **실측 결과**
    - 13:35Z STOP으로 재시작하는 데 30초 걸렸습니다.
    - 13:36:32Z에 rest 작업 프로세스를 **강제 종료**하자 supervisor가 **16.2초 만에 복구**했습니다(재시작 대기 10초 포함). 10단계의 강제 종료 후 복구 완료 조건을 충족합니다.
    - 첫 모니터 실행(13:38Z)에서 `ssd_monitor` incident를 열고 토스트를 보냈습니다(SSD 감시 작업 미등록).
    - 14:03Z에 두 번째 절전(11:39Z~)의 수신 공백 incident가 열리고 알림이 갔으며, 14:08Z에 닫혔습니다.
  - **F-012:** 수집기 gap 로그(`raw/_gaps`)를 카탈로그에 넣는 작업이 수동이어서, 11-08 14:41Z 이후의 restart gap이 빠져 있었습니다. 이제 컴팩션 자식 프로세스의 스캔에서 매번 가져옵니다(멱등).
- **3. Upbit 백필 작업 등록:** 자동 모드 분류기가 "무단 지속 실행"으로 거부했습니다(2회째). 우회하지 않았으며 사용자가 직접 실행해야 합니다(아래 명령).
- **4b. 상품·유니버스(설계 §5.3·§5.4)**
  - 리허설 중 `instrument`와 `universe_membership`이 0행인 것을 발견했습니다.
  - `catalog/universe_load.py`와 `tools/load_universe_catalog.py`로 상품 1,269개(Binance 969, Upbit KRW 300)를 적재했습니다.
  - 소속 구간 1,614개를 적재했습니다.
    - `binance.um.pit_top100.v1`: 1,507
    - `binance.um.collector_top.v1`: 71
    - `upbit.collector_krw.v1`: 36
  - 다시 실행하면 0개가 추가됩니다(멱등).
  - MATIC→POL 같은 계보 연결은 출처 목록이 필요해 이번 범위에서 뺐습니다.
- **4. T1 추가 데이터(8단계)**
  - 구성: `bulk/extras.py`(파서, 월 묶음 저장), `bulk/s3.py`, `tools/bulk_extras.py`, `tools/register_bulk_extras_task.ps1`
  - 파일럿(BTCUSDT·1000SHIBUSDT, 종류마다 최근 1개월)
    - funding 2개월 186행
    - metrics 2개월 61개 파일, 1만 8천 행
    - 현물 klines(BTCUSDT, SHIBUSDT) 2개월
    - bookDepth 2개월 60개 파일, 200만 행. 비공개 메모리 530MB였습니다.
  - **F-013:** bookDepth `percentage`가 2023년 파일은 `-5`, 2026년 파일은 `5.00`이었습니다. 파일럿에서 실패해 발견했고 Float64로 고쳤습니다.
  - 전체 실행은 klines와 같은 `run/bulk.lock`을 쓰므로 klines가 끝난 뒤에 진행됩니다. 작업 등록이 필요합니다.
- **5. 벌크와 REST 대조(8단계 완료 조건):** `tools/validate_bulk_klines.py`
  - seed 20261109: 40/40개 표본, 20,000봉 일치
  - seed 7: 60/60개 표본, 29,956봉 일치
  - 첫 실행(38/40)의 두 건은 상장 전 구간에 창이 떨어져 양쪽 모두 0봉이었습니다. 빈 비교는 계속 실패로 두고, 표본 창을 실제 데이터 범위 안에서 고르도록 고쳤습니다.
- **6. 7일 검증(11단계):** `tools/validate_week.py` → `docs/measurements/validate-week.md`
  - 중간 보고(1.16일) 커버리지: bookTicker 82.3%, Upbit 호가 68.9%, 강제청산 82.3%
  - 결측은 대부분 절전 2회(190분, 83분)와 11-08 Upbit 장애(225분)입니다.
  - OI·비율 30일 재수집에서 값이 바뀐 키는 0/약 62만이었습니다.
- **커밋과 CI:** d6dcdc8(run 42624331261), e8c09bc(42626757724), 223acb1(42627623060), 3d37186(42628316477), 741e3f1(42630262427), 1d0c690(42632224371) 모두 success
- **사용자 실행이 필요한 명령(일반 사용자 작업, 관리자 권한 불필요)**
  - `! powershell -NoProfile -File C:\dev\quant-research\tools\register_upbit_backfill_task.ps1`
  - `! powershell -NoProfile -File C:\dev\quant-research\tools\register_bulk_extras_task.ps1`

---

# Phase 2: DSL·백테스트 (계획 `docs/plans/2026-11-09-phase2-dsl-backtest.md`)

## Phase 2 상태: **핵심 구현 및 로컬 검증 완료 / 비용 모델 미보정** (2026-11-10 사용자 결정)
- **CI 갱신(2026-11-10):** GitHub Actions 장애가 끝난 뒤 다시 실행했고 모두 성공했습니다. b84efb7은 run 42671077766(4번째 시도), 6de82d7은 run 42673623592(2번째 시도)입니다. 장애 중의 실패 기록은 아래와 test-results에 그대로 남겨 둡니다.
- **최신 커밋 CI 미확인:** b84efb7(문서만 바꿈) CI run 42671077766이 3차례 job 0개로 실패했습니다.
  - 원인은 GitHub Actions 장애입니다(githubstatus.com "Incident with Actions", 2026-11-09 18:12Z부터, 18:3xZ에 major_outage).
  - 코드가 마지막으로 바뀐 커밋 1a2c533까지는 CI 성공(run 42670010290)입니다.
  - 장애가 풀리면 다시 실행하고 결과를 이 절과 test-results에 적습니다.
- **비용 모델 미보정:** ADR-021 사전값 그대로입니다.
- 아래 미검증·승인 대기 항목은 PASS로 처리하지 않습니다.
- **완료 기준(계획 §10)**
  1. **기준 사례 100%:** `tests/backtest/test_engines.py`에 손계산 기준 사례 7개가 있습니다(수수료·스프레드·재조정, 펀딩 롱·숏, 결측 시 거래 금지와 최악 가정, L0, 시간 손절, ATR 손절, 청산). 모두 1e-12 안에서 일치합니다. 두 엔진이 모두 지원하는 사례는 두 엔진 모두 확인했습니다.
  2. **엔진 교차 일치**
     - hypothesis 무작위 시나리오 300개: 자산 곡선, 비용, 펀딩, 결측 손실이 1e-9 안에서 일치
     - 템플릿 5종 × 합성 패널 전체 경로: 일치
     - 실데이터 스모크 5종: 최대 상대 차이 1.4e-14
  3. **시도 회계 누락 0:** `tests/research/test_ledger.py` 불변식 테스트. 실데이터 스모크는 평가 시도 20 = 새 trial 20 = backtest_run 20입니다.
  4. **연산자 누수 테스트:** 연산자 36개 모두 미래 값 주입 테스트를 통과했습니다. 패널 단계 주입(형성 중인 봉)과 봉인 위반 거부도 확인했습니다. S0 거부 규칙 테스트를 통과했습니다.
  5. **게이트 425 passed, 커버리지 93.72%**, CI 성공: run 42637257564(계획), 42638918593(1), 42641372698(2), 42642667952(3), 42643589847(4), 42644112134(5), 42646002172(6~7), 42667971088(8). 9단계(6f420ed)와 문서 커밋의 run id는 test-results에 적습니다.
- **미검증·제한(PASS 아님)**
  - **L2 체결:** aggTrades를 수집하지 않아 거부됩니다(trial `rejected`).
  - **비용 모델:** 보정하지 않은 사전값입니다(ADR-021). 스프레드 실측 보정은 7일 검증(OPS-007)과 2024-03 이전 bookTicker 벌크(보류) 이후에 합니다. 충격 계수 k는 설계상 보정할 수 없습니다.
  - **증거금·수수료:** 실제 구간을 쓰지 않았습니다(키 필요, Phase 6).
  - **펀딩:** 벌크 funding이 등록되지 않아(OPS-003, OPS-006) 실데이터 결과는 `funding_not_modelled`로 표시됩니다. funding_basis 템플릿은 거래가 0건입니다.
  - **패널 범위:** Binance USDⓈ-M만 지원합니다. 현물과 Upbit 패널은 미구현이며 S0가 거부합니다.
  - **사전 등록 값(§16-8):** ADR-022에 제안만 했고, 사용자 승인을 기다립니다. Phase 5에서 씁니다.
- **설계상 다른 Phase(이번 범위 밖, 구현하지 않음)**
  - 특징 평가표: Phase 4 S1 제안, 사용자 확인 필요
  - 탐색: S1~S3, TPE, TS, MAP-Elites는 Phase 4
  - validator, 홀드아웃, DSR, N_eff, StepM, 귀무 파이프라인은 Phase 4~5
  - Director, 메모리, 실패 원인 코드는 Phase 3

## 구현 파일
| 단계 | 파일 | 내용 |
|---|---|---|
| 1 | `adapters/sqlite.py`, `research/store.py`, `research/ledger.py`, `research/migrations/0001_trials.sql` | research.db(experiment, trial, backtest_run). 평가 전에 trial을 기록하고, 종료 상태를 불변으로 둡니다. 실험 키는 hash(정규 Spec + 데이터·lag·봉인·엔진·비용·시드)입니다 |
| 2 | `strategy/spec.py`, `ops.py`, `canonical.py`, `s0.py` | StrategySpec v1, 연산자 36개(lookback·데이터셋 선언), 깊이 8, 노드 64, window 2000, 상수 1e6, 정규형 해시, S0(현물 숏, maker, L2 필요, 유니버스, 최소 명목가, 비용 상한) |
| 3 | `backtest/panel.py`, `data/lake.py`(`load_bulk`), `data/access.py`(`check_decisions`) | 결정 격자, as-of 봉, 체결 구간(시가·고저·mark 고저), 구성원, 일별 유동성 상위 n 선택, 펀딩 정산 목록. 심볼 단위 적재 |
| 4 | `strategy/compile.py` | 시계열은 심볼별, 횡단면은 시점별 구성원만, 국면은 확장 창 분위 |
| 5 | `backtest/costs.py` | 수수료, Corwin-Schultz·Abdi-Ranaldo, 실측 스프레드, 제곱근 충격, 펀딩, 격리 청산가 |
| 6~7 | `backtest/market.py`, `backtest/engines.py` | 공통 준비(가격·범위·펀딩 행렬, 목표 가중치), 이벤트 엔진(수량·현금, 손절·시간 손절·청산), 벡터화 엔진(가중치, 상태 없는 규칙만, 청산 가능 표시) |
| 8 | `backtest/run.py`, `strategy/templates.py` | ledger 경유 실행, backtest_run 기록(일별 손익과 결측 최악 가정 손익 압축 저장), 거부 사유, 템플릿 5종 |
| 9 | `tools/run_backtest.py` | 실데이터 스모크(연구 구간, 자원·온도 게이트, research 잠금) |

## 실데이터 스모크 (2026-11-09 17:52Z, `docs/measurements/phase2-smoke.md`)
- **조건:** 8심볼(BTC, ETH, BNB, SOL, XRP, ADA, DOGE, LINK), 2025-01~06, 1h, 템플릿 5종, 이벤트 엔진(비용 ×1, ×2, ×3), 벡터화 엔진(×1)
- **처리량:** 결정 시점 4,344개, 패널 적재 3.7초, 최대 private 메모리 763MB
  - 첫 실행은 메모리 게이트(2GB 초과)로 멈췄습니다(F-014). 심볼 단위 적재로 바꾼 뒤 통과했습니다.
- **결과:** 모든 템플릿의 총수익이 0 이하입니다(비용 ×1에서 -1.2%~-13.4%). **성과 증거로 쓰지 않습니다**(고정 템플릿, 6개월, 비용 미보정, 펀딩 미모델).
- **관찰한 모델 특성:** 매 봉 목표 가중치로 재조정하는 규칙(ADR-020) 때문에 거래 수와 비용이 큽니다(trend 20,882건, 비용 258.7). 재조정 허용 범위(no-trade band)를 두는 것은 Spec 확장에 해당하므로 Phase 4 계획에서 결정합니다.

---

# Phase 3: Research Director, 상태 기계, Memory, LLM 연동 (계획 `docs/plans/2026-11-10-phase3-director-memory.md`, 2026-11-10 승인)

## 상태: **IMPLEMENTED / 가짜 LLM 10사이클 검증 완료 / 실제 LLM(구독 CLI) 3사이클 재검증 통과(F-016 수정 후) / 유료 API 비용 측정 DEFERRED(미승인)** (2026-11-10)
- 이전 상태 "FAILED_SCHEMA"는 F-016 수정(94c5c5f)과 재실행 r2로 해소됐습니다(아래 "실제 LLM 3사이클 재검증" 절).
- 유료 API 경로(`claude-api`)는 승인되지 않아 거부합니다. 실제 LLM은 구독 CLI 경로(`claude-cli`)만 씁니다(ADR-025).
- **범위 밖(구현 완료로 선언하지 않음):** Phase 4 대규모 탐색, Phase 5 독립 통계 검증. VALIDATE 상태는 기록만 하는 스텁이며, 홀드아웃에는 접근하지 않습니다.
- **주장 금지:** 10사이클 실행은 자율 연구 능력의 증명이 아닙니다. 전략 성과의 증거도 아닙니다.

## 구현 모듈 (커밋 2485f96, ccbb0ef, dd3a297, 74f38e0, cda7ea2, 그리고 이후 커밋)
| 모듈 | 파일 | 내용 |
|---|---|---|
| Memory | `research/memory.py`, `research/migrations/0002_memory.sql` | 근거 필수 발견(supersede 1회), 원인 코드 8종 실패, 쿨다운, 데이터 대기 차단(데이터 요청이 기록된 경우만), 익명 요약, 재시작 복원 |
| 익명화 | `research/anonymize.py` | 심볼 별칭(S01..), 날짜·epoch·실명 누출 탐지, 신뢰할 수 없는 텍스트 가림 |
| Phase 4 인터페이스 | `research/features.py` | `FeatureEvaluator` Protocol, `feature_eval` 스키마(ADR-023), 근거 참조 형식 |
| 스키마 | `director/schemas.py` | 가설·계획·해석의 닫힌 어휘, 길이 상한, 추가 필드 금지 |
| LLM | `director/llm.py`, `director/claude.py` | `LLMClient` Protocol, `FakeLLM`(결정적, 오작동 시나리오), `ReplayLLM`(재현), `ClaudeLLM`(공식 SDK `anthropic==2.7.0`, 승인 필요, 도구 없음, 구조화 출력, refusal fallback) |
| 예산 | `director/budget.py` | 공식 가격표(2026-11-10 확인), 사전 추정(fallback을 고려해 가장 비싼 모델 기준)과 사후 사용량 비용을 구분, 누적 $5·사이클 $2.5·호출당 토큰 상한, 초과 예상 시 호출 차단 |
| 상태 기계 | `director/states.py` | §10 전이표와 검사 |
| 실패 분석 | `director/failures.py` | 규칙 기반 원인 8종과 근거 trial |
| 정책 | `director/policy.py` | 계열 Beta 사후분포 Thompson, uniform 스위치, 원인 코드별 머묾·이탈 |
| Director | `director/director.py` | 사이클 실행기: 전이마다 이유와 근거를 트랜잭션으로 기록, 멱등 처리기, 재개, 중복 실험 생략, 예산·자원·온도 PAUSED, 누출 차단, 쿨다운·소진·데이터 대기 계열 제외, 모두 막히면 PAUSED |
| 공통 | `research/guard.py`, `backtest/run.py`(`experiment_config`, 워밍업 평가 창), `backtest/panel.py`(`slice_panel` 워밍업) | |
| 도구 | `tools/run_director.py` | 실데이터 무인 사이클, CPU·메모리·토큰·비용 측정 보고서 |

## 실데이터 실행 기록 (가짜 LLM, 8심볼, 선별 2025-01~04, 확인 2025-05~06, 워밍업 30일)
- **실행 6회, 사이클 57개, trial: S2 46 · S3 31(모두 done), 실행 중 잔류 0, 같은 실험을 두 번 끝까지 평가한 경우 0.** LLM 호출 136건은 모두 가짜이며 청구 0입니다.
- 앞선 실행에서 결함 4건을 발견해 고쳤습니다(F-015). 테스트를 먼저 만들었습니다.
- **최종 실행 `fake-s7-on-posterior-20261109T194855` (universe n=3), `docs/measurements/phase3-fake-10cycles.md`**
  - 10/10 사이클 done, 10사이클 모두 GENERATE → SEARCH → BACKTEST까지 진행
  - trial 24건, 중복 0
  - VALIDATE 스텁 도달 3회(Phase 3 확인 구간 기준이며 검증이 아님)
  - 시간 38.0초, CPU 40.2초, 최대 private 메모리 813MB
- **Memory 재사용 증거(전이 근거)**
  - 2번째 사이클부터 직전 원인 코드를 읽었습니다(`last_causes`).
  - weak_signal·regime_dependent 뒤에는 그 계열을 쿨다운했습니다(예: 3번 사이클 `cooled_down=['volatility']`).
  - cost_erosion 뒤에는 한 번 머물렀습니다(6번 사이클 "stay in mean_reversion once").
  - funding_basis는 데이터 요청을 기록한 뒤 차단됐습니다.
  - 이미 평가한 격자 점은 계획 단계에서 제외됐습니다(`excluded_variants`).
- **관찰:** 계열 5개에 쿨다운 5사이클이면, 남은 계열이 하나뿐인 경우가 자주 생깁니다(5~7번 사이클). 고정 격자 27점은 몇 번의 실행으로 소진됩니다. 탐색 공간 확장과 쿨다운 길이 조정은 Phase 4 계획에서 실측으로 정합니다.
- **사이클별 LLM 비용**
  - 사전 최악 추정: 평가 사이클 약 $0.48, 쿨다운 사이클 약 $0.16
  - 가짜 응답 토큰을 Opus 5.5 정가로 환산하면 약 $0.007입니다. 가짜 응답이 짧아 대표성이 없습니다.
  - 실제 비용은 유료 측정 전까지 미확인입니다.

## 유료 API 경로 (2026-11-10 사용자 결정: 키 설정과 유료 호출 미승인)
- `director/claude_api.py`(`ClaudeApiLLM`)는 API 전용 경로로 남겨 둡니다. 자동 모델 fallback은 기본으로 꺼져 있습니다.
- `tools/run_director.py --llm claude-api`는 실행 전에 거부합니다(종료 코드 2). `--approve-paid-calls` 플래그는 삭제했습니다.
- 계획 9단계(유료 비용 측정)는 열린 상태로 둡니다.

## 실제 LLM 3사이클: 구독 CLI 경로 (2026-11-10, run `cli-s7-on-posterior-20261110`, `docs/measurements/phase3-cli-3cycles.md`)
- **경로:** 수정하지 않은 `claude` 2.1.321을 `-p`로 실행합니다. `auth status`에서 `authMethod=claude.ai`를 확인했고, API 과금 환경 변수는 없었습니다.
- **호출 조건:** 도구, MCP, 설정, 세션 저장을 모두 끄고, fallback 없이 실행합니다. 기록된 청구는 0건입니다.
- **결과:** 3/3 사이클이 done이지만 **trial은 0건입니다.** 가설 호출 6건(사이클당 2회 시도)이 모두 `schema_invalid`였습니다.
  - 1차 시도: `statement`가 600자를 넘었습니다(3/3).
  - 2차 시도: 길이는 고쳤지만 `required_features` 이름에 숫자가 들어가 `^[a-z_]{1,32}$` 패턴을 위반했습니다(3/3). 예: `ema_50_close`, `ret_24h`.
  - 원인: `api_schema`가 값 제약(maxLength, pattern)을 지우고 보냅니다. 그래서 모델은 그 제약을 1차 시도에서 알 수 없었습니다. 2차 재시도에서는 직전 오류만 받았습니다.
- **7개 항목**
  1. 유효한 DSL 가설: 0/6. **실패**
  2. 실패 분석이 다음 결정에 반영됨: 재시도에서 직전 오류는 고쳤습니다(길이). 사이클 사이에서는 정책이 계열을 바꿨지만(trend → mean_reversion → momentum), 스키마 원인을 학습하지는 못했습니다. **미달**
  3. Memory 사용: 백테스트 결과가 없어 발견이나 원인 근거가 쌓이지 않았습니다. **검증 불가**
  4. 중복 실험 없음: trial 0이라 자명하게 0입니다. **검증 불가**
  5. 스키마 오류 복구: 오류가 나도 중단 없이 안전하게 폐기 → REPLAN → DONE으로 갔습니다. **안전성은 통과, 복구는 실패**
  6. 사용량 한도 안전 정지: 실제 한도에 도달하지 않았습니다. 가짜 실행기 테스트로만 검증했습니다. **실제 미확인**
  7. 가짜 LLM과의 차이: 가짜 응답은 스키마를 항상 지킵니다. 실제 모델은 길고 상세한 문장을 쓰고, 숫자가 든 특징 이름을 씁니다. 가짜 LLM 테스트가 이 실패를 잡지 못했습니다.
- **측정:** 호출 6건, 평균 지연 11.8초, 출력 토큰 4,608개. 전체 시간 83초, 최대 private 메모리 793MB.
  - 정가 환산 추정 $0.97은 사전 최악 추정의 합계입니다. 구독이라 청구액이 아닙니다.
- **구독 플랜:** CLI `auth status`는 `subscriptionType=pro`, `planTier=pro`로 보고합니다. 사용자는 Max라고 확인했습니다. 차이의 원인은 **미확인**입니다(로그인 시점에 저장된 값일 가능성).
- **정책 판단 근거:** 공식 headless 문서의 인터페이스이고, 사용자 본인의 로그인이며, 수정하지 않은 바이너리를 소규모로 씁니다. 개인 자동화를 명시적으로 허용하는 문구는 없으므로 이 부분은 **미확인**으로 둡니다(ADR-025).

## 실제 LLM 3사이클 재검증: F-016 수정 후 (2026-11-10, run `cli-s7-on-posterior-20261110-r2`, `docs/measurements/phase3-cli-3cycles-r2.md`)
- **조건:** 커밋 94c5c5f(프롬프트 phase3-v2), 같은 CLI 경로·호출 조건, 사이클 3개(사용자 승인 상한). 청구 0건.
- **결과(research.db 읽기 전용 조회로 확인, 사이클 61~63)**
  - LLM 호출 9건(가설·계획·해석 × 3) 모두 1차 시도 `ok`. schema_invalid 0, 재시도 0.
  - 가설 3건: trend, momentum, volatility. `statement` 440~456자(상한 600). 특징 이름은 모두 DSL 문법 이름(예: `ema_480_close`, `ret_24`, `rolling_max_24_high`).
  - trial 9건(사이클마다 S2 2 + S3 1, 모두 done), 실험 키 중복 0, 전역 done 중복 0, running 잔류 0.
  - 실패 원인 기록: 61 missing_data·weak_signal·regime_dependent, 62 missing_data·cost_erosion, 63 missing_data·cost_erosion·overfit.
  - 시간 132.6초, CPU 21.0초, 최대 private 메모리 683MB. 호출 지연 7.4~20.8초.
- **7개 항목 재판정**
  1. 유효한 DSL 가설: 3/3. **통과**
  2. 실패 분석이 다음 결정에 반영됨: 계열이 trend → momentum → volatility로 바뀌었고, 전이 근거에 직전 원인 코드가 남았습니다. 3사이클이라 학습 효과 판단에는 표본이 부족합니다. **부분 통과**
  3. Memory 사용: 사이클마다 원인 코드와 발견 1건(3사이클 합계 3건, 예: finding 33·34, 근거 trial 참조)이 기록됐고, 다음 사이클 OBSERVE 전이 근거에 `last_causes`로 읽혔습니다. **통과(소표본)**
  4. 중복 실험 없음: 0건. **통과**
  5. 스키마 오류 복구: 이번 실행에서는 오류가 없었습니다. 복구 경로는 가짜 LLM `too_long`·`unknown_feature` 시나리오 테스트로 검증했습니다. **테스트로 통과, 실제 미발생**
  6. 사용량 한도 안전 정지: 실제 한도 미도달. **실제 미확인**
  7. 가짜 LLM과의 차이: F-016 실제 응답 6건을 회귀 고정했습니다(`tests/director/fixtures/f016_real_answers.json`).
- **관찰(원인 미확인)**
  - 3사이클 모두 `missing_data`가 기록됐습니다. 가설의 `required_data`는 모두 `["klines"]`였고, 원인 근거는 `funding_not_modelled` 표시(OPS-003/006)입니다. 즉 가설이 아니라 펀딩 미모델 상태 때문에 붙는 원인입니다. 이 원인이 매 사이클 붙는 것이 정책 신호를 흐리는지는 Phase 4에서 판단합니다.
  - `exhausted`: trend는 1사이클 뒤, momentum은 2사이클 뒤 고정 격자 소진으로 제외됐습니다(이전 실행에서 평가한 점 포함). Phase 4 계획의 "27점 격자 한계" 1번을 실데이터로 다시 확인한 것입니다.
  - CLI가 보고한 `input_tokens`는 호출당 2이고 나머지는 `cache_read_tokens`(최대 1,638)입니다. 첫 사이클은 캐시 0입니다. CLI의 입력 토큰 집계 방식은 **미확인**이라 입력 토큰 실측값으로 쓰지 않습니다.
  - 측정표 헤더의 비용 문구는 `tools/run_director.py` 문구 수정 전 코드로 생성돼 "가짜 LLM" 문구가 남아 있었습니다. 수치는 그대로 두고 헤더에 정정 메모를 달았습니다.
- **Phase 4 반영:** 탐색 자체는 LLM 없이 돕니다. Director 호출은 사이클당 3회, 지연 약 10~45초/사이클을 예산 초깃값으로 씁니다.

---

# Phase 4: 대규모 탐색 (계획 `docs/plans/2026-11-10-phase4-search.md` 1~6단계, `docs/plans/2026-11-19-phase4-7-build.md` 1~6단계)

## 상태: **IMPLEMENTED / 완료 기준 2개 충족(회계 누락 0, 비용 예측 대역 비율 0.9 = 기준 경계)** (2026-11-19, 최종 배치 r4)
- **구현 파일:** `strategy/space.py`(사전 등록 공간, ADR-027), `search/`: `s0_stage`, `s1`·`s1_stage`(ADR-028), `promotion`·`s2_stage`(5% 무작위 승격, 이벤트 엔진 자동 전환 F-018), `cost_model`, `optimizers`(Random·TPE, ADR-029), `compare`, `multiplicity`·`neff_stage`(ONC·DSR, ADR-030), `bandit`(ADR-031), `archive`(MAP-Elites, ADR-032), `pipeline`(드라이버, ADR-033); `director/director.py` `search="space"` 모드; `tools/run_search.py`.
- **실데이터 배치(8심볼 1h, 선별 2025-01~04, 확인 05~06, 계열 5 × 탐색기 2 × S1 200):** `docs/measurements/phase4-search-batch.md`. 1차(`search-s7-20261119`, S2 벡터 엔진): trial 4,173 = 단계 합(누락 0), 비용 비율 대역 안 0.8, S2 178건 중 127건이 경로 의존 거부(F-018). 최종 `search-s7-20261119-r4`(S2 이벤트 자동 전환, ONC 벡터화 뒤): 10배치 604초, 최대 private 838MB, trial 4,173 = 단계 합(누락 0, running 잔류 0), S2 178/178 done, 비용 예측/실측 비율 20개 중 18개가 [0.5, 1.5] 안(**0.9**, 밖은 funding_basis 배치의 S1 1.73·S2 2.53), 수집기 gap 수 전후 동일(9,336), heartbeat 정상.
- **판정:** 두 기준 **충족**(비용 대역 비율은 정확히 0.9로 경계값. funding_basis는 S1 점수가 전부 미정의라 승격이 무작위 7~16건으로 다른 계열과 달라 예측이 어긋났다). 비용 모델은 배치마다 재적합하며 표본이 늘면 다시 측정합니다. **성과 주장은 없습니다**(모든 S3 보상 0 = 확인 구간 SR이 귀무 E[max] 이하, 창별 최고 S3의 DSR 보수값 0.04).
- **관찰:** funding_basis는 펀딩 미적재(OPS-006)라 S1 점수가 전부 미정의 → 무작위 승격만 S2로 갔습니다. N_eff는 선별 창 14(S2+ 639), 확인 창 2(S2+ 75)이고 N_total(S1 포함)은 약 8,000~8,600이라 DSR_total이 지배합니다. S1 대 S3 순위상관은 쌍이 2~4개뿐이라 판단 불가(trend −0.4, mean_reversion −1). 합성 Random 대 TPE는 3초 예산(약 36~40 평가)에서 둘 다 회수율 0 — 예산이 너무 작아 비교가 아니라 보고일 뿐입니다.
- **결함 기록:** F-018(S2 벡터 엔진 경로 의존 거부), ONC 성능(trial 300에서 수십 분 → 벡터화·k 상한 30, 18초).

---

# Phase 5: 독립 검증 (계획 `docs/plans/2026-11-19-phase4-7-build.md` 7~12단계)

## 상태: **IMPLEMENTED(라이브러리·도구) / 종단 FWER 점추정 0.04(200회) ≤ 0.05이나 95% 상한 0.077로 미확정 / 상시 validator 프로세스 없음** (2026-11-19)
- **귀무 검증(`docs/measurements/phase5-null-pipeline.md`):** random arm 3/100(0.03, Wilson [0.010, 0.085]), tpe arm 5/100(0.05, [0.022, 0.112]), 합계 8/200(0.04, [0.020, 0.077]). 사전 등록 목표 0.05를 점추정은 넘지 않지만 반복 수가 적어 확정 불가(ADR-022 예고). α/k=0.0025 기대치보다 통과율이 한 자릿수 높은 점은 **미확인 원인**(합성 귀무가 24h 블록 내부 자기상관을 보존 → 완전 귀무 아님 가능성 / 부트스트랩 SE 과소 / 스트레스 조건 상관)으로 기록했고, 규칙을 완화하지 않고 최종 검증 단계에서 shuffle 귀무로 재측정합니다.
- **검정력(알파 주입, arm당 30회):** random 드리프트 0.0015/h → 2/30·1/30, mechanism 평균회귀 0.015 → 0/30·1/30. 귀무 통과율과 구별되지 않아 **검정력 ≈ 0**(심은 알파가 α/k 기준에 비해 너무 약함). 강도·구간을 키운 재측정이 다음 항목이며 기준값은 바꾸지 않습니다.
- **구현 파일:** `validator/store.py`+`migrations/0001`(ADR-034), `validator/verdict.py`(ADR-035), `validator/stepm.py`(ADR-035), `validator/null_pipeline.py`+`tools/run_null_pipeline.py`(ADR-036), `validator/lifecycle.py`+`migrations/0002`·`validator/prior_knowledge.py`(ADR-037), `research/migrations/0003_validation.sql`(outbox), `DirectorConfig.anonymize`.
- **테스트:** store 5(예산 k·재질의 차감·1비트 ack·해시 체인 변조 탐지·research 역할 holdout 거부), verdict 6(손계산·귀무 기각률·부트스트랩 결정성·손익분기), stepm 4(귀무 FWER ≤ α, 알파 검출, medoid), null_pipeline 4, lifecycle 6.
- **측정:** `docs/measurements/phase5-null-pipeline.md`(100회 반복, Random·TPE arm). Director(가짜 LLM) arm은 별도.
- **범위 밖:** 실제 holdout(2026-07-01~11-01 벌크)에 대한 질의는 아직 0건입니다(k=20 예산 미사용). 사전 등록 값 ADR-022는 그대로이며 확정 제안은 Phase 5 문서 단계에서 합니다.

---

# Phase 6: 모의매매 (계획 13~17단계)

## 상태: **IMPLEMENTED / 30일 운영은 사용자 작업 등록 대기 / 대상 전략 0개** (2026-11-19)
- **구현 파일:** `paper/ledger.py`+`migrations/0001_ledger.sql`(해시 체인 원장, 멱등 client_id, UNCERTAIN 조회만), `paper/engine.py`(호가 표본 체결·부분 체결·관통 규칙), `paper/feed.py`(bookTicker → 1분 mid 봉 → 패널), `paper/divergence.py`(신호·지연·스프레드·부분 체결 분해, 봉인 전 요약), `paper/monitor.py`(CUSUM·슬리피지·gap 저하 → Degraded), `paper/run.py`(프로세스), `risk/limits.py`·`risk/policy.py`(ADR-038), `tools/register_paper_task.ps1`, 모니터 `OPTIONAL_HEARTBEATS`.
- **테스트:** ledger·engine 7, divergence·monitor 3, feed·tick 2, risk 4.
- **미완료:** 30일 운영(사용자가 `register_paper_task.ps1` 실행 후 시작), 읽기 키 기반 수수료·증거금(보수값 유지, 미확인), 대상 전략이 생기려면 Phase 5 validator가 실제 holdout 판정을 내려야 합니다(현재 0건).

---

# Phase 7 준비: 노트북 쪽 코드 (계획 18~21단계)

## 상태: **코드 준비 완료 / 실행은 사용자 결정 대기** (2026-11-19)
- **구현 파일:** `live/signing.py`(ed25519, `cryptography==50.0.2`), `live/orders.py`, `live/deadman.py`, `live/testnet.py`(요청 형식, `unverified`), `docs/live-rehearsal.md`(사용자 결정 7개와 리허설 7단계). ADR-039.
- **테스트:** signing 2, orders·deadman·testnet 5.
- **하지 않은 것(설계대로):** 키·실거래 호스트·법률 확인·실주문·테스트넷 호출.

# v2.2 재구축 (`docs/plans/2026-11-20-v22-rebuild.md`)
## 상태: **IN_PROGRESS** (2026-11-20)
- **A0 (01f2f8c, CI 44622826560):** v2.2 설계 개정판(`docs/plans/2026-11-20-autonomous-quant-research-v2.2.md`), ADR-040~048 사전 등록. 2020-01~2024-11 데이터는 아직 연구 역할로 읽지 않았다.
- **A1 (모의매매 피드 메모리):** `paper/feed.py` `LakeFeed`가 lake 파일 하나씩 부분 1분봉으로 줄여 프로세스 수명 동안 캐시하고 병합한다. 원시 호가는 미체결 주문의 체결 창(가장 이른 도착 −5분 ~ 현재)만 읽는다. `tick(feed=...)`은 `MarketFeed` 프로토콜을 받는다. 실측(실제 lake, 45일 창 = 수집된 231파일 628MB, 8심볼): 최초 8.0초·캐시 재호출 0.14초, 프로세스 private 237MB(`POLARS_MAX_THREADS=2`; 기본 스레드 수는 약 1GB), 메모리에 동시에 올린 원시 행 최대 180,000(파일 1개). 종전 방식은 231파일 전체를 한 번에 concat. 예약 작업은 스레드 2개를 먼저 정하는 `python -m quant_research.paper`(`paper/__main__.py`)로 실행. 계획과의 차이: 캐시는 디스크(`paper/cache`)가 아니라 프로세스 메모리(부분 봉은 45일 8심볼 약 8만 행) — 쓰기 경로를 늘리지 않기 위해.
- **독립 리뷰(Phase 4~7 변경분, `code-reviewer`):** High 5·Medium 6·Low 9건 보고. 확인·수정은 다음 단계(리뷰 수정)에서 테스트 먼저.
- **리뷰 수정 커밋:** ac730ed(validator), 360d282(search), 2984ad9(live), edb43aa(paper, A1 포함), 71c7260(docs) — CI 44663936702 success. 리뷰 4회(F-019). 마지막 0행 세그먼트 수정은 4차 리뷰가 제시한 수정이며 5차 리뷰 없이 커밋했다(hook 1회 차단 후 재실행).
- **A2 (판정 규칙 v2):** `DecisionRule`에 사전 조건(활동일 ≥ 60, 독립 진입 ≥ 30, 평균 총노출 ≥ 5%, holdout ≥ 600일)과 PSR 병행. 미달은 `Verdict.insufficient` → `register_and_judge`가 제출을 거부하고 질의를 쓰지 않는다. 보정표 `docs/measurements/phase5-verdict-calibration.md`: v2.1(부트스트랩만, 60일) 거짓양성 0.008~0.33 → v2(600일) 밀집 0.0012·AR(0.3) 0.0036, 700일 전 귀무 ≤ 0.0027(상한 0.00375). 250일은 상한 초과(0.0055)라 최소 길이를 600일로 사전 등록(ADR-042 보강). 남은 한계: 강한 자기상관에서 상한 근접.
- **A2 커밋:** a28235a, CI 44669238136. 리뷰 반영(진입 수 기준, 하니스의 불충분 분리) 뒤 재리뷰 없이 커밋(hook 1회 차단 후 재실행).
- **A3 (종단 FWER 하니스):** 89dca8e, CI 44674612084. `replicate_epoch`(전 계열 탐색 → 확인 SR 양수 생존자를 SR 순으로 k회까지 판정, 불충분 미차감, epoch당 통과 여부), `EpochResult`(후보 0 epoch = 측정된 미통과, 전부 불충분 = 측정 불가), 합성 시장 길이 지정·귀무 3종(부트스트랩·블록 순열·블록 부호 뒤집기)·단순수익률 중심화, 알파 강도는 거래 가능한 전략(전 심볼 매수 / 직전 시간 역행)의 총 시간 z를 목표로 시뮬레이션 보정, `tools/run_null_v2.py`(epoch마다 설정 포함 jsonl). 리뷰 2회: 기본 귀무가 base 기간 drift를 심던 문제(700일 'null'에서 z≈2.9), 해석식 강도의 과대(심볼 상관 0.76), k 불일치·null 이름 오타·분모 정의 등 수정. 측정(부호 뒤집기 귀무 200 epoch) 진행 중: 이 측정은 수정 전(로그수익률 중심화) 코드로 돌고 있으며, 그 편향은 FWER를 높이는 쪽이라 상한 ≤ 0.05면 결론은 유효하다(재리뷰).
- **A4 (bandit 보상 분리):** bandit이 있으면 S1·S2는 선별 구간 앞 75%에서, 최고 S2 후보를 뒤 25%에서 다시 평가(`fold '<fold>:val'`, 별도 trial)해 그 결과로만 보상한다(`split_for_reward`, `_reward_run`, `batch_reward`). Director space 모드는 bandit을 파이프라인에 넘기고 확인 구간(S3) 결과로는 갱신하지 않는다. 테스트: S3가 있든 없든 보상이 같다.
- **A4 커밋:** 9bd3346(A3 후속: 단순수익률 중심화·귀무별 보정), 94e76b3(A4), CI 44678021445. 리뷰 반영(보상 귀무 SR 0, val trial 회계, 메모리 가드, 최소 분할) 뒤 재리뷰 없이 커밋.
- **A6·A7 (e2d22aa, 로컬):** S3 = 상위 `s3_top` + 나머지 S2 생존자의 무작위 5%(`s3_random_rate`), 배치 도구 기본 `--s3-top 5`. 비용 예측은 평가된 trial 각각의 rows·ops와 실제 S2 건수로(`SearchResult.predicted`). 실데이터 배치 재측정(순위상관 쌍 수, 대역 비율)은 귀무 측정이 끝난 뒤.
- **A8:** `tools/run_director.py --search space`(+ `--s1-count`, `--top-k`). 구독 CLI로 실데이터 3사이클: 3/3 done, 사이클당 약 31초, 청구 0(`docs/measurements/phase4-director-space-cli.md`). 측정표의 `done_duplicates 188`은 DB 전체(이전 배치들) 누적 지표이며 이 실행의 중복 생략은 0이다.
- **A9:** 모의매매가 `pit_top_liquidity`를 아예 적용하지 않던 결함(전 구성원 매매 vs 백테스트는 상위 n) 수정: `target_weights`가 `select_top_liquidity`를 쓰고, 순위 기준은 bookTicker 표본 수가 아니라 직전 완결 월의 bulk 1분봉 **거래** quote volume(`LakeFeed.liquidity`, 최대 1개월 지연). `paper/run.py` 루프·`approved_strategies` 테스트 추가. Upbit 체결 모델은 T6 데이터 뒤로 이월.
- **A10 (OPS-009):** 열린 critical incident는 6시간마다 재알림(catalog 마이그레이션 0007 `last_notified_ns`), supervisor가 자식별 연속 단기 실행 횟수를 `run/supervisor.state.json`에 쓰고 모니터가 10회 이상이면 `restart_loop:<child>` critical, `register_paper_task.ps1`이 `run/paper.expected`를 써서 등록 뒤에는 paper heartbeat가 없어도 감시.
- **A11:** `neff_groups(fold_prefix=)`로 실행별 N_total을 누적과 따로 보고(DSR은 누적 유지). S0/S1 재평가 회피는 F-019(재사용 S1)로 이미 반영.
- **A12:** ONC — 완전 상관 집단은 N_eff 1, k 상한 `onc_k_cap(n) = max(30, ⌈2√n⌉)`(≤ n−1). 배치 도구 DSR 표는 귀무 분산 `sr_variance(0, skew, kurt, T)`, 합성 Random 대 TPE 비교는 예산 60초 이상일 때만(기본 끔). 정책 HMAC 키 평문은 ADR-048에 위험 수용으로 기록됨.
- **A6~A12 리뷰 수정:** (1) 거래량 없는 심볼이 0으로 순위에 들던 문제 → `select_by_liquidity`(값이 있는 구성원만 상위 n, 동률은 이름순), 거래량 자료가 전혀 없으면 해당 전략은 결정하지 않고 포지션 유지(`no_liquidity`), (2) 다운로드 중인 월 → 심볼마다 가장 최근 완결 월, (3) 회복한 자식의 연속 재시작 횟수 초기화(실행 120초 이상) + 60초 주기 상태 갱신 + 10분 넘은 상태 무시, (4) 상태 파일 쓰기는 PermissionError 시 제자리 쓰기, 어떤 OSError도 supervisor를 멈추지 않음, (5) `s3_random_rate` 기본 0(배치 도구만 0.05), 표본 S3 시간은 bandit 보상 비용(`reward_cost_stages`)에서 제외, (6) 비용 예측은 이번 배치에서 새로 평가한 trial만, (7) paper 작업 해제 절차(operations §17), (9) ONC k 상한 최대 40(`MAX_K_CEILING`; n=400에서 30→45초). 남은 한계: 상한 40은 독립 trial이 40개를 넘어도 N_eff ≤ 40으로 둔다(누적 N_total DSR이 보수 쪽 하한).
- **B1 (S1 최소 표본):** 이벤트 또는 IC 결정 < 30이면 점수 −∞, 이상이면 t·n/(n+30)(`S1Result.score`). 충실도 표는 저장된 점수 대신 현재 규칙으로 재계산.
- **B2 (봉인 epoch 2, 코드):** 위 ADR-040 보강. 리뷰 반영: event가 holdout인 행이 available만 연구 창이면 보이던 경계 문제, 이전 백업 스냅샷(seal_window 없음) 복원 검증이 죽던 문제. 운영 catalog에는 마이그레이션 0008만 적용됨(seal_window 0행); 등록(`--apply`)은 귀무 측정 종료 뒤.
- **B2 등록 완료 (2026-11-20):** 귀무 측정(구 코드) 종료 뒤 `tools/register_seal_epoch2.py --apply` 실행, exit 0. 9개 데이터셋(spot klines, um book_depth·funding·index·klines·mark·metrics_5m·premium, upbit.candles_1m)이 epoch 2, 연구 창 2개. 재실행 dry run은 대상 0(멱등). 2차 리뷰 반영: 수신 시계가 늦어 available < event인 holdout 이벤트가 연구 창 끝에서 보이던 문제(event < 창 끝도 요구), 충실도 테스트가 `fidelity_by_family`를 직접 검사.
- **A3 부호 뒤집기 귀무(89dca8e 코드):** 200 epoch, 측정 159, 후보 0인 epoch 10, 측정 불가 41, FWER 0.000(Wilson 95% [0.000, 0.024]), 평균 질의 2.3, 불충분 비율 0.50 → 기준(상한 ≤ 0.05) 충족. `docs/measurements/phase5-null-v2-signflip.md`. bootstrap·permute 100회씩 진행 중(B2 코드).
- **B3 (패널 저장소·PIT 유니버스, D7):** `backtest/panel_store.py`. 심볼별 shard를 `load_panel`로 만들고(봉인·PIT 규칙 그대로) float32 parquet로 `qr-data/cache/panels`에 캐시한다. 키 = sha256(심볼, 창, 패널 설정, 규칙, 역할, 멤버십, 데이터셋별 시간 의미·유효/최신 lag, 봉인 창, lake 파일(경로·크기), 생성 코드 소스, polars 버전). 행은 첫 as-of 입력부터 마지막 체결 구간(+마지막 봉을 처음 보는 결정)까지 유지하고 구성원 공백 행도 남긴다(행 기반 피처). `load_universe_panel(top_m)`: 각 달의 거래 유니버스 = 월초 구성원 중 전월 거래대금(그 달 첫 결정 시점에 보이던 봉만) 상위 M, 창의 첫 달은 웜업(`evaluate_from_ns`). 측정 도구 `tools/build_panels.py`(연구 게이트로 중단 시 exit 3, 재실행이 이어받음). 리뷰 2회 반영: 공백 행 삭제로 shift/rolling이 공백을 건너던 문제, 키에 lag 누락(재보정 뒤 옛 shard 재사용 = 미래 정보), 임시 파일 충돌, 파일 목록 불일치, 월초 멤버십, `Window.months` 경계(ns-1이 float에서 다음 달로 반올림).
- **B3 실측 (2026-11-20):** 창 2(2024-12~2026-07) 1h top-100: 438심볼, 빌드 533초(최대 1.07GB), 적재 42초(최대 1.58GB), 249만 행(383MB). 창 1(2020-01~2023-01) 1h top-100: 155심볼, 빌드 470초(최대 1.52GB), 적재 24초(1.57GB), 240만 행(371MB). 캐시 합계 0.44GB. `docs/measurements/b3-w2-1h.md`, `b3-w1-1h.md`. 적재 최대가 연구 게이트 2GB에 가깝다: 1h 셀은 창별·top-M별로 한 번에 하나.
- **A3 귀무 측정 (B2 코드, 100 epoch씩):** bootstrap 판정 79·통과 0(Wilson 95% 상한 0.046), permute 판정 81·통과 0(상한 0.045), 앞서 signflip 159·0(상한 0.024). 세 귀무 모두 기준(상한 ≤ 0.05) 충족. `docs/measurements/phase5-null-v2-bootstrap-permute.md`. 검정력(z = 2·4·6, random·mechanism, 30회씩) 실행 중.
- **B4 (데이터 tier, 코드):** (1) bootstrap이 ADR-013 epoch 1에 남은 벌크 데이터셋을 즉시 epoch 2로 옮긴다(새 tier도 처음부터 H_hist 봉인; 동시 실행 경쟁은 no-op). (2) T5 `bulk/flow.py`: aggTrades 월 파일을 체크섬 검증하며 디스크로 스트리밍, 64MB 조각 단위 1분 테이커 흐름 집계(매수·매도 수량·금액, 체결 수, 10만 USDT 이상 대형 체결 금액), 원본 zip 삭제, 데이터셋 `binance.um.bulk.agg_flow_1m`. (3) `tools/bulk_extras.py`: `--kinds aggTrades`, tier 옵션 `--top N`·`--since YYYY-MM`(검증), tier 실행도 `bulk.lock` 공유. (4) T7 `bulk/external.py` + `tools/fetch_external.py`: Fear & Greed, Coin Metrics Community, Deribit DVOL(키 불필요, 조사 `docs/measurements/b4-t7-sources.md`), 문서화되지 않은 공개 시각은 보수적 lag로 등록. (5) catalog 마이그레이션 0009: dataset venue에 `external` 추가(테이블 재구성, 외래 키 검사 후 커밋; 행 내용 불변). **실제 다운로드는 미실행**: 이 세션의 백그라운드 다운로드가 권한 검사로 거부되어 사용자 실행 명령으로 넘긴다.
- **B4 리뷰 반영 (2차):** Coin Metrics 잠정(flash) flow 지표 제외(오늘 받은 확정치를 D+2에 알 수 있던 값으로 쓰는 편향), 일부만 다시 받은 달은 이전 버전과 병합(새 값 우선), 버전 이름 ns 단위·기존 이름 거부, DVOL 미확정 캔들 제외, 파서 오류를 `ExternalSourceError`로, `load_dataset`이 외부 데이터셋도 최신 버전만, bootstrap 등록 경쟁 허용(`tolerate_race`), 파일럿 전용 aggTrades 작업 폴더, `--limit-months` 검증. 운영 catalog는 이미 user_version 9(마이그레이션 0009 적용; 검정력 측정이 실데이터 기본 패널을 읽으며 catalog를 열 때 적용된 것으로 보임). 리뷰어가 읽기 전용으로 foreign_key_check 빈 결과·integrity ok·dataset 25행·trigger 11개를 확인.
- **B5 (공간 재설계, D5):** 위 ADR-046 보강 2. 엔진 리밸런스 주기·무거래 밴드(두 엔진, 기준 사례 + 일치 속성 테스트), 명세 필드(기본값이면 해시 불변), 주기 불일치 거부, 공간 v2 15개와 `space_of`(v1 조회 경로 그대로), S3 최소 노출.
- **B6 (계열 확장, D6):** 위 ADR-046 보강 3. 새 계열 6개(총 11, v2 공간 33개), 기준 사례 테스트(합성 효과 회수 t > 3, 랜덤 워크 |t| < 3). 데이터가 필요한 4계열은 보류로 기록.
- **B7 (자율 캠페인 러너, D8):** 위 ADR-047 보강. 순수 핵심과 구동기를 테스트로 확인했다(원인 분류 표, 전이, 은퇴, 큐 우선, 재시작 후 같은 결정, 중단 후 재개 시 평가 trial 중복 0, 음성 결과와 다음 tier). 실행 도구 `tools/run_campaign.py`를 추가했다.
- **B7 리뷰 반영:** ADR-047 보강의 "리뷰 반영(B7)" 12건(폐포 BFS, research 잠금, 귀무 기대 최대 SR 기준, S2-S3 짝 맞춤, bandit 척도, 배치 오류 기록, 재개 설정 고정, 시간 예산 종료 코드, 경로·run-id, 웜업 상한, 표 이스케이프, 무작위 탐색).
- **B8 (validator 프로세스):** 위 ADR-041 보강. `validator/process.py`(제출·대기 요청·판정·거부·최종 비트), 도구 2개. 테스트: S3만 1회 제출, 실제 규칙에서 짧은 holdout은 거부되고 예산을 쓰지 않음, 판정 시 1비트만 ack에 남고 holdout trial은 research.db에 없음, 접근 로그 체인 검증, 다른 규칙 요청 거부, 기원별 최종 비트 표.
- **A3 완료 (2026-11-20):** 귀무 3종 FWER 기준 충족, 검정력은 드리프트 z=6에서 61%, 메커니즘 0%(4계열 공간에 1봉 되돌림 계열 없음 → `xs_reversal` 포함 재측정이 후속). `docs/measurements/phase5-power-v2-summary.md`.
- **B11 (모의매매 인프라 점검):** `tools/paper_smoke.py`. 실제 lake의 최근 12시간을 1시간마다 13틱 돌렸다. 더미 전략은 v1 trend, 동일 가중, top-5이고, 정책 키는 일회용, 원장은 `qr-data/paper/smoke-*`의 새 원장이다. 주문 28건을 내고 lake 호가로 체결했다. 대사 불일치 0, 원장 해시 체인 정상, 남은 미체결 1건은 마지막 틱의 주문(체결 구간을 lake가 아직 덮지 않음)이다. `docs/measurements/b11-paper-smoke.md`. 예약 작업 등록(`tools/register_paper_task.ps1`)과 실제 정책 키는 사용자 항목이다.
