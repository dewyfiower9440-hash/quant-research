---
status: in-progress
task: qr-phase3
---

# Phase 3: Research Director, 상태 기계, Memory, LLM 연동 (설계 v2.1 §13 Phase 3)

> 2026-11-10 사용자 지시로 작성한 계획입니다. **승인 전에는 구현하지 않습니다.**
> - LLM API 키 설정, 유료 호출, 새 의존성(`anthropic` SDK)은 각각 별도 승인을 받습니다.
> - 외부 데이터·서비스 추가와 실거래는 범위 밖입니다.

## 1. 원본 v2.1 기준 Phase 3 정의와 현재 상태 비교

### 1-1. 설계 요구 사항과 구현 현황
| 설계 근거 | 요구 사항 | 현재 구현 | Phase 3 작업 |
|---|---|---|---|
| §13 Phase 3 행 | 상태 기계, 익명화, LLM. 완료 기준 "가짜 LLM 10사이클, 사이클당 비용 실측". 외부 API는 Claude API(승인) | 없음 | 전부 |
| §4 모듈 A (Research Director) | 목표, 가설, 우선순위, 해석, 재계획. 입력은 익명 메모리 요약 + 카탈로그 + 예산, 출력은 `Hypothesis`/`ExperimentPlan`/`DataRequest`. 스키마 위반 시 1회 재요청 후 폐기·기록, 예산 초과 시 PAUSED. 완료 기준: 무인 10사이클, 중복 실험 0, 모든 전이에 근거 기록 | 없음 | 전부 |
| §4 모듈 I (Research Memory) | 가설, 실험, 실패, 발견. `finding(evidence_refs NOT NULL, superseded_by)`, `failure(cause_code)`. 쓰기 실패 시 루프 정지. 마이그레이션. 완료 기준: 재시작 후 복원 | research.db(experiment, trial, backtest_run) | 메모리 테이블 추가 |
| §10 상태 기계 | OBSERVE → HYPOTHESIZE → PLAN → (ACQUIRE_DATA) → GENERATE → SEARCH → BACKTEST → VALIDATE → ANALYZE → LEARN → REPLAN, PAUSED. 원인 코드별 전이 정책, 계열 사후분포가 LLM보다 우선, 실험 키 중복 방지, 기각 가설 쿨다운, 전이·작업 트랜잭션, 품질 저하 지표 | 실험 키는 있음(Phase 2) | 상태 기계, 정책, 쿨다운, 품질 지표 일부 |
| §6 1번 | 실패 원인 코드 8종(신호 약함, 비용 잠식, 국면 의존, 용량 부족, 데이터 결측, 과최적화, 실행 불가, 사전지식 의심) | 없음(trial에 거부 사유만 있음) | 규칙 기반 분류기 + LLM 해석 |
| §8 기록 필드 | trial마다 `model_id`와 LLM 학습 기준일. LLM이 메모리를 보고 낸 가설도 trial | trial에 해당 열 없음 | 마이그레이션으로 열 추가 |
| §9-6 | 익명화 여부에 따른 성과 차이 기록, 판정은 학습 기준일 이후 구간에서만 | 없음 | 익명 대 실명 기록 필드(측정 자체는 Phase 5) |
| §9-9 | Phase 3부터 `memory=off`, `policy=uniform` 스위치 | 없음 | 스위치 |
| §3.1, §6 5번, §15 | Director의 네트워크는 Claude API로만 제한. 도구 없는 요약기. LLM 출력은 데이터이며 지시가 아님 | 없음 | LLM 어댑터 허용 목록, 도구 미제공, 스키마 검증 |
| §12 | 외부 Claude API, 최고 성능 모델, 익명 컨텍스트, 비용은 Phase 3에서 실측 후 상한 | 없음 | 비용 계측과 상한 |

### 1-2. 범위 밖 (원본 단계 정의 기준)
- **데이터 발굴 모듈 B:** Phase 5 이후입니다. Phase 3의 ACQUIRE_DATA는 `data_request`를 outbox에 기록만 하고 REPLAN으로 갑니다(사람 승인 큐도 Phase 5 이후).
- **탐색 S1~S3, TPE, TS, MAP-Elites, N_eff, DSR:** Phase 4입니다. Phase 3의 SEARCH는 사이클마다 미리 정한 소수의 변형만 평가합니다(예산 상한 있음).
- **validator, 홀드아웃 1비트, StepM, 귀무 파이프라인:** Phase 5입니다. Phase 3의 VALIDATE는 "validator 미구현"을 기록하고 ANALYZE로 갑니다. 홀드아웃에는 접근하지 않습니다(research 역할).
- **IC 특징 평가표:** Phase 4 S1입니다(ADR-023). Phase 3에서는 인터페이스만 설계합니다.

## 2. 재사용 (Phase 2)
- **StrategySpec, S0, 정규형 해시, 템플릿 5종:** GENERATE 단계입니다. LLM은 템플릿, 연산자, 이유만 고르고 수치는 고정 후보에서 고릅니다(§7 "수치는 최적화기", 최적화기는 Phase 4).
- **`load_panel` + `run_backtest` + `TrialLedger`:** BACKTEST 단계입니다. 모든 평가는 trial입니다(거부 포함).
- **실험 키(`experiment_key`):** 중복 실험을 막습니다.
- **연구 구간 분할:** 선별과 확인으로 나눕니다(§8 데이터 분할). 예: 연구 구간 안에서 앞쪽 70%를 선별, 뒤쪽 30%를 확인으로 씁니다. 봉인 홀드아웃은 PIT API가 거부합니다.

## 3. 모듈 구조
```
src/quant_research/
  research/
    migrations/0002_memory.sql   hypothesis, plan, cycle, transition, finding, failure, llm_call,
                                 data_request, alias(익명화 대응표), feature_eval(스키마만, ADR-023),
                                 trial에 model_id / llm_cutoff / anonymized 열 추가
    memory.py      MemoryStore: 발견(근거 참조 필수, superseded_by), 실패(원인 코드), 가설 쿨다운,
                   익명 요약(봉인 이전 데이터만), 재시작 복원
    anonymize.py   심볼 → 별칭(A01..), 시각 → 상대 인덱스, 가격 → 수익률·표준화 값만.
                   대응표는 research.db에만 두고 프롬프트에 넣지 않습니다
    features.py    FeatureEvaluator Protocol, FeatureEvalRecord(Phase 4에서 구현)
  director/
    states.py      State enum, 전이표(§10), 가드. 순수 함수
    schemas.py     Hypothesis, ExperimentPlan, Analysis, DataRequest(pydantic, 크기 상한, extra 금지)
    llm.py         LLMClient Protocol, LLMRequest/LLMResponse(usage, 비용, 지연, model_id, 거부)
                   FakeLLM: 결정적 시나리오(정상, 스키마 위반, 중복 가설, 데이터 요청, 거부, 예산 초과)
                   ClaudeLLM: 공식 anthropic SDK, claude-opus-5-5(사용자 승인 후에만 생성 가능)
    budget.py      호출 수, 토큰, USD 상한(사이클별, 전체) → PAUSED
    failures.py    지표 → 원인 코드(규칙 기반, 근거 trial id 포함)
    policy.py      원인 코드별 다음 행동, 계열별 Beta 사후분포(LLM 제안과 다르면 사후분포 우선).
                   스위치: memory=off, policy=uniform
    director.py    사이클 실행기: 상태마다 트랜잭션으로 전이를 기록하고, 다시 시작하면 마지막 전이에서 재개
tools/run_director.py   --llm fake|claude --cycles N --budget-usd X(claude는 승인 플래그가 없으면 거부)
```

### 인터페이스 요지
- `LLMClient.complete(request: LLMRequest) -> LLMResponse`
  - 요청에는 스키마 이름, 익명 컨텍스트, 출력 상한이 들어갑니다.
  - 응답은 원문을 보관하고, 검증된 모델로만 사용합니다.
  - 도구는 제공하지 않습니다. 구조화 출력(`output_config.format`)을 쓰고, 강제 `tool_choice`는 쓰지 않습니다(Opus 5.5에서 400).
- `Director.run_cycle(cycle_id) -> CycleResult`: 각 상태는 멱등이고, 같은 사이클을 다시 실행하면 이어서 진행합니다.
- `MemoryStore.summary(scope) -> AnonymizedSummary`
  - 연구 구간 결과만 담고, 심볼과 날짜 문자열은 들어가지 않습니다(누출 테스트).
  - `memory=off`이면 빈 요약을 돌려줍니다.
- `Hypothesis` 필드: `family`, `statement`, `falsification_test`, `required_data`, `required_features`(Phase 4 연결), `prior`, `anonymized=1`

## 4. 구현 순서 (각 단계: 테스트 먼저 → 구현 → 게이트 → 커밋 → CI)
- [x] 1. research.db 마이그레이션 0002와 MemoryStore
  - 테스트: 근거 없는 발견 거부(NOT NULL과 검사), supersede 체인, 원인 코드 열거, 재시작 후 복원, 쓰기 실패 시 예외 전파(루프 정지)
- [x] 2. 익명화
  - 테스트: 대응표 왕복, 프롬프트 페이로드에 실제 심볼·날짜·가격 수준이 없음(누출 탐지), 별칭 고정성
- [x] 3. 스키마와 FakeLLM
  - 테스트: 스키마 위반 시 1회 재요청 후 폐기·기록, 크기 상한, 거부(refusal) 처리
- [x] 4. 상태 기계와 정책
  - 테스트: 전이표 전체(허용·금지 전이), 원인 코드별 전이, 사후분포 갱신과 우선, 쿨다운, uniform 스위치
- [x] 5. 실패 분류기
  - 테스트: 원인 코드 8종의 합성 지표 사례, 근거 trial id 포함
- [x] 6. Director 사이클 실행기와 예산
  - 테스트: 정상 사이클, 예산 초과 PAUSED, 데이터 요청 → outbox → REPLAN, VALIDATE 스텁, 중간 종료 후 재개 시 trial 중복이나 누락 0
- [ ] 7. 가짜 LLM 무인 10사이클(실데이터 패널: 이미 받은 8심볼, 연구 구간) → 측정 보고서
- [x] 8. ClaudeLLM 어댑터: **새 의존성 `anthropic` 승인 후**
  - SDK 1.x는 httpx2를 사용합니다.
  - 테스트는 Protocol 수준의 가짜 클라이언트로만 합니다(네트워크 0).
- [ ] 9. 실제 API 비용 측정: **키 설정과 유료 호출 승인 후**
  - 상한 $5로 3~10사이클을 실행하고, 사이클당 토큰과 비용을 실측합니다.
  - 승인이 없으면 이 단계는 PENDING으로 두고, Phase 3 상태를 `IMPLEMENTED / LLM_COST_MEASUREMENT_PENDING`으로 기록합니다.
- [ ] 10. 문서(progress, ADR, failures, operations, test-results, 측정표)

## 5. 검증 기준 (Phase 3 완료 판정)
1. **가짜 LLM 무인 10사이클:** 사람 개입 없이 10사이클이 끝나거나, 정책상 PAUSED로 정상 종료합니다.
   - 모든 상태 전이에 근거(rationale, evidence_ref)가 기록됩니다.
   - 같은 실험 키의 의도하지 않은 재평가가 0건입니다.
   - 평가 시도 = trial 수입니다(거부 포함).
2. **재시작 복원:** 사이클 도중 강제 종료한 뒤 다시 시작하면 마지막 전이에서 이어서 진행합니다. trial 중복이나 누락은 0건입니다.
3. **견고성:** 스키마 위반, 거부, 예산 초과, 데이터 요청 시나리오가 모두 정의된 전이로 처리됩니다(가짜 LLM 시나리오 테스트).
4. **격리**
   - 익명화 누출 테스트를 통과합니다.
   - 아키텍처 테스트: director·research 모듈은 validator·holdout 경로를 import하지 않습니다. LLM 어댑터 외에는 네트워크 클라이언트를 만들지 않습니다.
   - 봉인 구간 질의는 거부됩니다(기존 PIT 테스트).
5. **ablation 스위치:** `memory=off`, `policy=uniform`이 동작합니다(테스트).
6. **실제 Claude API 비용 측정:** 승인 후 최소 3사이클을 실행해 사이클당 입력·출력·캐시 토큰, USD, 지연, 스키마 위반률을 측정합니다. 이 결과로 Phase 4의 호출 빈도와 예산 상한을 정합니다.
7. 게이트 통과, CI 성공(run id 기록)
8. **주장 금지:** 이 Phase의 어떤 결과도 수익성이나 통계적 유의성의 증거로 쓰지 않습니다. validator가 없고, 비용이 보정되지 않았고, ADR-022는 초기값입니다.

## 6. 예상 자원 사용량
- **CPU·RAM**
  - Director 자체는 가볍습니다(수십 MB).
  - 백테스트는 Phase 2 실측값을 기준으로 잡습니다(8심볼 6개월 패널 763MB, 평가 1회 수 초).
  - 패널은 사이클 사이에 다시 씁니다. 사이클당 평가 상한은 초깃값 6회(변형 3 × 이벤트·벡터 교차 1)로 둡니다.
  - 가짜 LLM 10사이클은 수 분 이내로 추정합니다.
  - 수집기와 벌크 작업을 위해 단일 프로세스로 실행하고, 자원·온도 게이트와 `run/research.lock`을 씁니다.
- **디스크:** research.db 증가는 사이클당 수십 KB로 추정합니다(LLM 원문 응답 포함).
- **네트워크:** 가짜 LLM은 0입니다. 실제 호출은 api.anthropic.com만 사용합니다.

## 7. 외부 비용 (승인 필요, 추정)
- **모델:** `claude-opus-5-5`(skill 기본값이자 사용자 원칙 "최고 성능"). 백만 토큰당 입력 $4, 출력 $20, 캐시 읽기 $0.20(Anthropic 1차 API 가격, skill 캐시 2026-10-06 기준, 공식 표 재확인 필요)
- **사이클당 추정:** 호출 3회(가설, 계획, 해석)
  - 입력 약 12k 토큰 × 3 = 36k → $0.14
  - 출력(사고 토큰 포함) 약 4k × 3 = 12k → $0.24
  - 합계 **약 $0.4/사이클**입니다. 고정 시스템 프롬프트를 캐시하면 입력 비용이 줄어듭니다.
  - 실측 전 추정이며 effort 설정에 따라 크게 달라질 수 있습니다.
- **측정 실행 제안:** 3~10사이클, 상한 **$5**(budget.py가 강제)
- **장기:** 사이클 수 × 실측 단가로 Phase 4 예산을 다시 정합니다(설계 §16-7 "Claude API 월 예산은 Phase 3 실측 후 결정").

## 8. 위험과 대응
| 위험 | 대응 |
|---|---|
| 프롬프트 인젝션(LLM 출력·외부 텍스트) | LLM에 도구를 주지 않습니다. 출력은 pydantic 스키마로 검증한 데이터로만 씁니다. 지시로 실행하지 않습니다. 외부 웹 텍스트는 Phase 3에서 쓰지 않습니다 |
| LLM 사전지식 누수(BTC 가격 수준 등으로 식별) | 가격 수준을 빼고 수익률·표준화 값만 줍니다. 별칭을 쓰고 날짜는 상대 인덱스로 바꿉니다. 익명 여부를 기록하고, 측정·판정은 Phase 5에서 합니다 |
| 반복 탐색에 의한 과최적화 | 모든 평가를 trial로 셉니다. 확인 구간을 분리합니다. 유의성은 주장하지 않습니다. 다중검정 보정은 Phase 4~5에서 합니다 |
| 비용 초과 | 사이클별·전체 USD 상한, 호출 수 상한, 초과 시 PAUSED |
| 비재현성(LLM은 확률적이고 Opus 5.5는 temperature를 지원하지 않음) | 프롬프트 해시, 원문 응답, model_id, 학습 기준일, 사용량을 저장합니다. 재현은 저장된 응답 재생으로 합니다 |
| Opus 5.5 API 특성 | thinking을 끌 수 없어 effort로 제어합니다(기본 medium). 강제 tool_choice는 400이므로 구조화 출력을 씁니다. refusal stop_reason을 처리하고, 서버 측 fallback은 사용자에게 고지한 뒤 사용합니다 |
| 키 유출 | 키는 Credential Manager 또는 환경 변수에서 런타임에 읽고, 로그·DB·프롬프트에 남기지 않습니다(사용자 설정, 승인 후) |
| Phase 1·2 미검증 의존 | 결과에 `funding_not_modelled`, 비용 미보정, OPS 이슈 표시를 그대로 붙입니다 |
| 새 의존성 공급망 | `anthropic`과 그 의존성(httpx2)을 승인 후 추가하고, pip-audit과 uv.lock으로 관리합니다 |

## 9. 승인이 필요한 항목
1. 이 계획(Phase 3 착수)
2. 새 의존성 `anthropic`(공식 SDK) 추가: 8단계
3. Anthropic API 키 설정 방식과 유료 호출, 측정 예산 상한(제안 $5): 9단계
