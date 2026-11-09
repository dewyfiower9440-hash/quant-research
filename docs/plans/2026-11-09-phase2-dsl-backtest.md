---
status: in-progress
task: qr-phase2
---

# Phase 2: DSL·백테스트 (설계 v2.1 §13 Phase 2)

> 2026-11-09 사용자 지시 "Phase 1 정리 및 Phase 2 개발 착수"에 따라 진행합니다.
> - Phase 1은 **IMPLEMENTED / OPERATIONAL_VALIDATION_PENDING**입니다. 미완료 항목은 OPS-001~007(`docs/operations.md` §11)로 추적합니다.
> - 이 계획 범위 안의 개발·테스트만 수행합니다.
> - 새 외부 서비스 연결, 유료 데이터, 시스템 권한 변경, 실거래 관련 작업은 하지 않습니다(별도 승인 대상).

## 1. 원본 v2.1 기준 Phase 2 정의
- **§13 표:** "2 DSL·백테스트 | Spec, 두 엔진, 비용 모델, **시도 회계 시작** | 작업자 1~2 × 2GB | 외부 API 없음 | 완료 기준: **기준 사례 100%, 엔진 교차 일치** | 실패 시: 기준 사례 추가"
- **관련 절**
  - §7: StrategySpec, 연산자, 상한, S0 정적 검사, 정규형 해시
  - §9: 비용·체결 모델, `backtest_run.fidelity` L0/L1/L2와 최소 요건 표("v2.1 추가(Phase 2)")
  - §8: 시도 회계 불변식
  - §4: 모듈 E, G의 완료 기준
  - §1-2: "시도 회계도 Phase 2부터"
  - §12: 얇은 자체 엔진, Numba는 경로 의존 로직용
- **§16-8 사전 등록 항목**은 "Phase 2 전에 문서화"하라고 돼 있습니다. 대상은 홀드아웃 비율·epoch(완료: ADR-011, ADR-013), 전역 질의 예산 k, 종단 FWER 목표, 수축 계수 c, 기본 lag, 귀무 반복 수입니다. 이 값을 실제로 쓰는 곳은 Phase 5(validator)입니다. 그래서 **제안값을 ADR 초안으로 문서화하고 사용자 승인 대기**로 둡니다(10단계). Phase 2 구현을 막지는 않습니다.

## 2. 요청 기능의 설계상 위치 (원본 단계 정의 기준. 모두 Phase 2라고 가정하지 않음)

| 기능 | 설계상 위치 | Phase 2에서 할 일 |
|---|---|---|
| Feature Discovery 및 피처 생성 | 모듈 D. §13 로드맵에 **단계가 명시돼 있지 않습니다.** 특징 평가표(IC, HAC t)는 S1 대리 지표(§8, Phase 4)와 같은 계산입니다 | DSL이 쓰는 **특징 연산자 라이브러리**(lookback·lag 선언)와 연산자별 누수 테스트만 합니다. 특징 평가표(`feature_eval`)는 하지 않습니다(Phase 4 S1 범위 제안, 사용자 확인 필요) |
| Strategy Factory와 DSL | 모듈 E, §7. **Phase 2**("Spec") | 구현합니다: Spec, 연산자, 상한, S0, 정규형 해시, 템플릿 |
| 전략 후보 생성 및 탐색 | §8 S1~S3, TPE, TS, MAP-Elites: **Phase 4**. 계열 템플릿은 "Phase 2~3" | 하지 않습니다. 템플릿 5종(추세, 평균회귀, 모멘텀, 펀딩·베이시스, 변동성)을 고정 파라미터로만 둡니다. 엔진 검증용이며 탐색이 아닙니다 |
| 백테스트 엔진 | 모듈 G. **Phase 2**("두 엔진") | 벡터화 엔진(선별용)과 이벤트 엔진(기준용) |
| 수수료·슬리피지·체결 모델 | §9 비용 표. **Phase 2**("비용 모델") | 수수료, 스프레드(실측·추정량), 충격 사전값과 k 스트레스, 펀딩, 청산 판정, 결측 정책. 증거금 구간과 필터는 보수적 값입니다. L2(aggTrades)는 데이터가 없어 거부합니다 |
| 시점 정합성·누수 방지 | Phase 1 구현(PIT API, 봉인). Phase 2 엔진은 **PIT API만으로** 데이터를 받습니다 | 엔진·연산자 단위 누수 테스트(미래 값 주입, 구간 시작 시각 주입, 봉인)를 둡니다 |
| 독립 검증과 과최적화 방지 | 모듈 H, §9 validator, 1비트 홀드아웃, DSR·N_eff·StepM, 귀무 파이프라인: **Phase 4~5** | 하지 않습니다. 대신 **시도 회계**(Phase 2 "시작")를 구현합니다. 평가 1회는 trial 1건이고, 실패·중단도 기록합니다. Phase 4~5의 다중검정이 이 기록을 입력으로 씁니다 |
| 실패 분석과 다음 연구 반복 | 모듈 A·I, §10 상태 기계, 메모리, 원인 코드: **Phase 3** | 하지 않습니다. trial 상태와 실패 사유(예외, 데이터 없음, S0 거부)는 기록합니다 |

## 3. 기존 구현 조사

| 구분 | 내용 |
|---|---|
| 이미 구현된 기능 | PIT `PointInTime.join_asof`/`rows_visible`(lag 정책, 봉인), catalog `lag_policy`·`seal_policy`·`universe_membership`(PIT top-100)·`instrument`(승수), 벌크 klines 4종(진행 중), 벌크 REST 대조 100/100, exchangeInfo 스냅샷, bookTicker 1초(1.2일치) |
| 재사용 가능 | `data/lake.py`(파일 단위 읽기, 최신 버전 선택), `catalog/store.py` 마이그레이션 패턴(번호 SQL + `user_version`), `bulk/guard.py`(자원 게이트), `capture/thermal.py`, `adapters/jobhost.py`(메모리, 로그, 원자적 JSON), `bulk/klines.missing_minutes` |
| 미구현 | `research.db`, trial 기록, StrategySpec·연산자·S0·정규형, 패널 빌더, 비용 모델, 두 엔진, `backtest_run`, 템플릿 |
| Phase 1 미완료에 의존 | 펀딩·베이시스 계열 실데이터(bulk funding 미등록, OPS-003·006), 전체 유니버스 백테스트(klines 70%, OPS-006), 스프레드 실측 보정(7일 검증 미완료, OPS-007), Upbit 전략(분봉 백필 미등록, OPS-003) |
| 데이터 부족으로 검증 불가 | L2 체결(aggTrades 미수집, T2), 충격 계수 k 보정(메타오더 정보 없음, 설계상 보정 불가), 2024-03 이전 스프레드 보정(bookTicker 벌크 보류, ADR-018), 지연 분포(모의매매 Phase 6), 실제 수수료·증거금 구간(키 필요, Phase 6), maker·지정가 전략(모의매매 전용, §9) |

## 4. 설계 결정
- **ADR-020 엔진: 얇은 자체 엔진(설계 §12 유지)**
  - NautilusTrader는 펀딩·증거금을 지원하는 이벤트 엔진입니다. 그러나 PIT 봉인 API, 버전 lag, 두 거래소 필터, 시도 회계와 결합하려면 같은 작업이 다시 필요합니다. vectorbt는 행렬 단위 계산이라 펀딩 정산·시점 정합 모델링이 어렵습니다(웹 조사 2026-11-09, 미확인 부분 있음).
  - **이벤트 엔진**은 순수 Python 루프로 시작합니다. 실측이 느리면 Numba로 바꿉니다(CLAUDE.md §9: 측정 기반).
- **research.db:** 소유자별 SQLite 원칙(§3.1)에 따라 `meta/research.db`로 둡니다. 쓰는 쪽은 research 프로세스 하나이고 catalog와 분리합니다. 마이그레이션은 별도 폴더에 둡니다.
- **시도 회계 불변식**
  - `TrialLedger.evaluate()`가 평가 **전에** `running` trial을 기록하고, 끝나면 `done`/`failed`/`rejected`로 바꿉니다.
  - 다시 열 때 남아 있는 `running`은 `aborted`로 닫습니다.
  - 상태 변경은 running → 종료 상태만 허용합니다(SQL 트리거).
  - 적합도 평가는 이 경로로만 실행합니다(아키텍처 테스트로 엔진 직접 호출 검사).
- **실험 키:** hash(정규화 Spec, 데이터셋 버전, lag 정책 버전, 봉인 epoch, 엔진·fidelity·비용 설정, 시드)(§10)
- **결정 시점과 체결**
  - **결정 격자:** timeframe(예: 15m)의 경계 d_k에서, 그 시점에 **available인 봉만** 씁니다(PIT 규칙: 1m 봉 available = open + 1m + lag).
  - **L0:** 결정 시점 직전에 available인 봉의 종가로 체결하고 비용은 없습니다. 연구용 상한이며 채택 판단에 쓰지 않습니다.
  - **L1:** d_k + 지연(기본 1분 = 다음 1m 봉) 시가로 체결합니다. 수수료, 스프레드 절반, 충격, 펀딩, 청산 판정(봉 안의 mark 고가·저가)을 반영합니다.
  - **L2:** aggTrades가 필요하므로 `FidelityUnavailable`로 거부하고 trial을 `rejected`로 기록합니다.
- **최소 fidelity 요건(§9-5):** 보유가 1시간 미만이거나 참여율이 1%를 넘으면 L2가 필요합니다. 그래서 현재 데이터로는 실행하지 않고 `rejected`(사유 기록)로 둡니다. maker 체결 방식은 모의매매 전용이라 거부합니다.
- **결측 정책:** gap 구간(카탈로그 gap, 1분봉 누락)에서는 신규 주문을 하지 않습니다. 기본 결과와 함께 **최악 가정 버전**(gap 안 보유분에 불리한 극단 이동 적용)을 따로 보고합니다.
- **엔진 교차 범위:** 벡터화 엔진은 상태가 없는 규칙(조건 → 목표 포지션)만 지원합니다. 시간 손절, ATR 손절, 청산 같은 경로 의존 규칙은 이벤트 엔진 전용입니다. 교차 일치는 두 엔진이 모두 지원하는 Spec 집합에서 확인합니다.
- **수수료 기본값(보수적, 공식 표 미확인)**
  - Binance USDⓈ-M VIP0: 테이커 0.05%, 메이커 0.02%(Binance FAQ 예시값)
  - Binance 현물: 0.1%
  - Upbit KRW: 0.05%(설계 §9)
  - 스트레스 배수는 ×1, ×2, ×3입니다.

## 5. 모듈별 구현 순서 (단계)
- [x] 1. **ADR-020과 시도 회계**
  - 내용: `research/store.py`(research.db, 마이그레이션 0001: experiment, trial, backtest_run), `research/ledger.py`(evaluate 컨텍스트, 실험 키, 남은 running 정리)
  - 테스트(먼저): 평가마다 trial 1건, 예외는 `failed`이고 사유를 기록, 중단(running 잔존)은 다시 열 때 `aborted`, 종료 상태는 변경 불가(트리거), 같은 실험 키 재실행도 trial 1건 추가(재평가도 셈)
- [x] 2. **StrategySpec DSL**
  - 내용: `strategy/spec.py`(pydantic, 파싱 전 크기·중첩 검사), `strategy/ops.py`(연산자 레지스트리: 종류, 인자 수, window, lookback, 필요 데이터셋), `strategy/canonical.py`(정규형·sha256), `strategy/s0.py`(정적 검사)
  - 테스트: hypothesis 문법 property(유효 AST 생성 → 파싱 → 정규형 → 재파싱 동일), 교환 연산 정규형 동치, 상한(깊이 8, 노드 64, window, 상수) 경계값, 무효 거부 사유, S0 거부(현물 숏, 현물 롱숏 횡단면, 예상 비용 상한, 최소 명목가)
- [x] 3. **선택적 레이크 읽기와 패널 빌더**
  - 내용: `data/lake.py` `load_bulk(dataset, symbols, months)`(필요한 파일만, 최신 버전), `backtest/panel.py`(1m → timeframe 봉, 봉 available = 구성 1m 행의 available 최대값, 결정 격자 as-of, PIT 유니버스 구성원 마스크, 결측 마스크)
  - 봉인 검사는 PIT API의 공개 메서드로 합니다(access.py에 결정 시점 검사 메서드를 분리하고 join_asof도 이를 사용).
  - 테스트: 봉 available, 봉인 위반 거부, 구성원 마스크, 결측 봉, 읽은 파일 수(필요한 파일만)
- [x] 4. **연산자 컴파일러와 누수 테스트**
  - 내용: `strategy/compile.py`. 특징·신호를 Polars 식으로 바꿉니다(시계열은 심볼별 over, 횡단면은 시점별 구성원만, 국면은 확장 창 분위).
  - 테스트: 연산자마다 손으로 계산한 값, **연산자마다 미래 값 주입 누수 테스트**(T 이후 값을 바꿔도 T 이전 출력이 같음), 구간 시작 시각 주입(봉 시각을 시작으로 바꿔 넣으면 탐지), lookback 워밍업은 null
- [x] 5. **비용 모델**
  - 내용: `backtest/costs.py`(수수료, 스프레드: 실측 bookTicker 또는 Corwin-Schultz·Abdi-Ranaldo 추정, 충격 Y·σ·√(Q/ADV)·k, 펀딩 현금흐름, 스트레스 배수)
  - 테스트: 추정량 공식 손계산 사례, 비용 단조성(배수 증가 → 비용 증가), 펀딩 부호(롱이 양수 펀딩을 냄), 정산 시각에만 발생
- [x] 6. **이벤트 엔진(기준)과 기준 사례**
  - 내용: `backtest/event.py`(봉 루프, 상태, 진입·청산, 시간 손절, ATR 손절, vol_target 사이징, 레버리지 상한, 펀딩 정산, mark 고가·저가 청산, 결측 시 거래 금지, 최악 가정 버전)
  - `tests/backtest/test_reference_cases.py`: 손으로 계산한 기준 사례(수수료, 스프레드, 펀딩, 손절, 시간 손절, 청산, gap, 횡단면, PIT lag)
- [x] 7. **벡터화 엔진과 교차 일치**
  - 내용: `backtest/vector.py`(상태 없는 규칙)
  - 테스트: 같은 기준 사례 100%, hypothesis로 만든 상태 없는 Spec × 합성 패널에서 두 엔진의 일별 손익이 1e-9 안에서 일치, 미래 값 주입 시 두 엔진 모두 과거 손익 불변
- [x] 8. **실행기, backtest_run, fidelity, 템플릿**
  - 내용: `backtest/run.py`(ledger 경유 실행, backtest_run에 fidelity·데이터 버전 해시·lag 버전·봉인 epoch·env_id·git SHA·비용 설정 기록, 일별 손익 벡터 압축 저장, 지표: 기간 SR, 회전율, MDD, 거래 수, 손익분기 비용 배수), `strategy/templates.py`(5계열)
  - 테스트: 기록 필드, L2·최소 요건 거부, 템플릿 5종 S0 통과, 실행 → trial·backtest_run 1:1
- [x] 9. **실데이터 스모크(연구 구간만, 성능 주장 없음)**
  - 내용: `tools/run_backtest.py`(자원·온도 게이트, POLARS_MAX_THREADS=2). 이미 받은 심볼 몇 개 × 템플릿(펀딩 계열은 파일럿 funding이 있는 구간만)으로 두 엔진의 교차 일치와 trial 회계를 실측합니다. 결과는 기록만 하고 성공 판정에 쓰지 않습니다.
- [ ] 10. **문서**
  - 사전 등록 ADR 초안(§16-8, 사용자 승인 대기): k, FWER 목표, c, 귀무 반복 수, 기본 lag 확인
  - progress(Phase 2 절), decisions, failures, test-results, CLAUDE.md(명령·구조)

## 6. 데이터·인터페이스 의존성
- **입력:** `PointInTime`(research 역할), catalog `universe_membership`(`binance.um.pit_top100.v1`), `instrument`(승수), `gap`, 레이크 `binance.um.bulk.{klines,mark_klines,premium_klines}_1m`, `binance.um.bulk.funding`(파일럿 범위), (선택) `binance.um.book_ticker_1s` 실측 스프레드
- **출력:** `meta/research.db`(experiment, trial, backtest_run). catalog에는 쓰지 않습니다(단일 작성자 원칙).
- **인터페이스:** `StrategySpec` → `compile(spec, panel)` → 신호 → 엔진(`VectorEngine`/`EventEngine`).`run(spec, panel, costs, fidelity)` → `BacktestResult` → `TrialLedger`.

## 7. 검증 게이트
- 단계마다 선언 게이트(`uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q --cov`)를 실행하고, 커밋·push 후 CI를 확인합니다.
- tools/*.py는 `uv run pyright tools/<file>.py`를 따로 실행합니다(Gotcha).
- 테스트 안 네트워크 호출 0, 고정 시드, 합성 데이터

## 8. 예상 자원 사용량
- **테스트:** 합성 패널(심볼 ≤ 5, 봉 ≤ 5,000)로 1GB 미만. hypothesis 예제 수는 단계별로 제한합니다(기본 100~200).
- **스모크:** 심볼 5~10 × 1~2년 1m(약 500만~1,000만 행)을 심볼·월 단위로 읽고 15m로 집계해 1~2GB를 넘지 않게 합니다.
  - POLARS_MAX_THREADS=2, 결과 패널만 메모리에 둡니다.
  - 실행 전 `bulk/guard`(가용 RAM 1.5GB, 자기 메모리 1GB → 스모크는 2GB로 설정)와 thermal 게이트를 통과해야 합니다.
- **CPU:** 수집기(실시간)와 벌크 다운로드(BelowNormal)가 함께 돌기 때문에 스모크는 단일 프로세스로 실행합니다.

## 9. 기존 기능과의 충돌 가능성
- **catalog.db:** research는 읽기만 합니다. `Catalog.open`은 마이그레이션을 실행할 수 있으므로, 연구 쪽 읽기는 기존 열기 방식을 쓰되 쓰기 메서드를 호출하지 않습니다. read-only 열기 함수를 추가할지는 3단계에서 판단합니다.
- **레이크 파일:** 불변 파일을 읽기만 하므로 벌크 다운로드와 충돌하지 않습니다. 읽는 중에 새 버전이 생기면 실행 시점의 파일 목록과 버전 해시를 backtest_run에 기록합니다.
- **`access.py` 수정(봉인 검사 메서드 분리):** Phase 1 PIT 테스트가 회귀 방지 역할을 합니다.
- **수집기와 벌크 프로세스:** 중단하지 않습니다. 이 계획은 수집기 코드를 바꾸지 않습니다(OPS-004는 별도 계획).

## 10. 완료 판정 기준 (Phase 2 DONE)
1. 기준 사례 100%: 모든 기준 사례가 두 엔진(지원 범위)에서 손계산 값과 1e-12 안에서 일치합니다.
2. 엔진 교차 일치: hypothesis Spec 200개 이상 × 합성 패널, 그리고 실데이터 스모크에서 일별 손익 차이가 1e-9 이하입니다.
3. 시도 회계 누락 0: 테스트 불변식, 그리고 스모크 실행 수 = trial 수 = backtest_run 수(rejected 포함)
4. 연산자 전체 누수 테스트 통과, S0 거부 테스트 통과
5. 게이트 통과, CI 성공(run id 기록)
6. 남은 항목(L2, 보정되지 않은 비용 요소, Phase 1 미완료 데이터)은 PASS로 처리하지 않고 미검증으로 명시합니다.
