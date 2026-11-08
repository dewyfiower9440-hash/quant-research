# 진행 현황 (Phase 0~1)

> 기준: `docs/plans/2026-11-08-phase0-1-build.md`(실행 계획, 승인 2026-11-08) / 설계 v2.1
> 상태 값: TODO / IN_PROGRESS / DONE / PARTIAL / BLOCKED. DONE은 완료 조건, 선언 게이트, CI를 모두 통과했을 때만 씁니다.
> 재개 방법: 이 파일에서 첫 번째로 DONE이 아닌 작업을 찾고, 실행 계획의 같은 단계 번호를 함께 읽습니다.

| # | 작업 | 상태 | 마지막 갱신 |
|---|---|---|---|
| 1 | Scaffold와 저장소 | DONE | 2026-11-08 |
| 2 | Phase 0 측정 | DONE (CI run 42469337357) | 2026-11-08 |
| 3 | 원본 캡처기 최소판 | DONE (CI run 42480240871) | 2026-11-08 |
| 4 | 조기 백업과 복원 리허설 | TODO (다음) | |
| 5 | 카탈로그 스키마 | TODO | |
| 6 | as_of / join_asof API | TODO | |
| 7 | 컴팩션과 정규화 | TODO | |
| 8 | Binance 벌크 T1 | TODO | |
| 9 | Upbit 분봉 백필 | TODO | |
| 10 | 모니터와 작업 스케줄러 | TODO | |
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
