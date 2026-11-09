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
