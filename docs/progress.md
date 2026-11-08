# 진행 현황 (Phase 0~1)

> 기준: `docs/plans/2026-11-08-phase0-1-build.md`(실행 계획, 승인 2026-11-08) / 설계 v2.1
> 상태 값: TODO / IN_PROGRESS / DONE / PARTIAL / BLOCKED. DONE은 완료 조건, 선언 게이트, CI를 모두 통과했을 때만 씁니다.
> 재개 방법: 이 파일에서 첫 번째로 DONE이 아닌 작업을 찾고, 실행 계획의 같은 단계 번호를 함께 읽습니다.

| # | 작업 | 상태 | 마지막 갱신 |
|---|---|---|---|
| 1 | Scaffold와 저장소 | DONE | 2026-11-08 |
| 2 | Phase 0 측정 | DONE (CI run 42469337357) | 2026-11-08 |
| 3 | 원본 캡처기 최소판 | DONE (CI run 42480240871) | 2026-11-08 |
| 4 | 조기 백업과 복원 리허설 | PARTIAL (외부 대상 미정) | 2026-11-08 |
| 5 | 카탈로그 스키마 | DONE (CI run 42531815458) | 2026-11-09 |
| 6 | as_of / join_asof API | DONE (CI run 42536249442, F-007로 재확정) | 2026-11-09 |
| 7 | 컴팩션과 정규화 | DONE (CI run 42536249442) | 2026-11-09 |
| 8 | Binance 벌크 T1 | TODO | |
| 9 | Upbit 분봉 백필 | TODO | |
| 10 | 모니터와 작업 스케줄러 | 스케줄러 부분 완료(재부팅 검증 대기), 모니터 TODO | 2026-11-08 |
| 11 | 7일 연속 운영 검증 | TODO | |

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
- **미해결:** 외부 백업 대상. 연결된 외장 디스크가 없고, OneDrive는 사용할 수 있지만 용량을 확인하지 못했습니다. 사용자 결정이 필요합니다.

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
