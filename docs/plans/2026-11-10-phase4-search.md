---
status: in-progress
task: qr-phase4-search
---

# Phase 4 대규모 탐색 계획 (설계 기준: v2.1 §8, §13 4행)

## Context
- Phase 3가 2026-11-10 마감됐습니다(커밋 2ec62d9). 실제 LLM(구독 CLI) 3사이클 재검증 r2에서 LLM 9/9 1차 시도 통과, trial 9건, 중복 0이었습니다. 유료 API 비용 측정은 미승인으로 DEFERRED입니다.
- r2에서도 trend는 1사이클, momentum은 2사이클 만에 27점 고정 격자가 소진됐습니다. Phase 3 Director는 숫자를 고정 격자에서만 고를 수 있어 탐색이 멈춥니다.
- 사용자가 2026-11-10 "phase4 작업진행해"로 착수를 지시했습니다. 이 문서는 그 초안(`docs/plans/2026-11-10-phase4-search.md`)에 변경 범위와 단계 1 상세를 더한 승인용 판본입니다. 승인되면 같은 내용을 그 파일에 씁니다.
- 별도 승인 항목은 그대로 남습니다: Optuna(단계 6), scipy(단계 7, 필요 시), 작업자 2개(단계 10), `git push`.

## 목표와 완료 기준 (§13 4행)
- 목표: S0~S3 단계 파이프라인, N_eff(ONC), 계열별 Thompson Sampling, MAP-Elites.
- 완료 기준(객관적)
  - **비용 예측 ±50%:** 단계별 `cost_s` 예측 대비 실측 비율이 0.5~1.5 안에 들어야 합니다(배치 단위 90% 이상).
  - **회계 누락 0:** 적합도 평가 1회마다 trial 1건이 기록돼야 합니다. Optuna 내부, 특징 선택, 민감도 분석, 중단된 실행도 모두 포함합니다.
- 비목표
  - 전략 채택, validator, 봉인 홀드아웃 사용, StepM은 Phase 5입니다.
  - GA, 6번째 이후 계열은 다루지 않습니다.
  - 실거래는 하지 않습니다.
  - 이 Phase의 어떤 결과도 성과 증거로 보고하지 않습니다.

## 현재 상태 (실측, 2026-11-10 research.db 읽기 전용 조회)
- 시도: S2 done 46건(평균 `cost_s` 1.34초, 최대 1.49), S3 done 31건(평균 0.49초), smoke 20건. 중복 0입니다. (r2 이후 S2 52, S3 34)
- `feature_eval` 테이블은 있지만 0행입니다. S1 IC 표는 미구현입니다.
- `backtest_run.pnl_daily`(zlib float64, 일간)가 S2 이상 시도마다 저장돼 있습니다. N_eff 입력으로 쓸 수 있습니다.
- `trial.stage`는 S0~S3를 이미 허용합니다. `cost_s`도 기록 중입니다.
- 패널 1회 적재는 3.7~10.7초, 최대 private 메모리는 683~827MB입니다(8심볼, 6개월, 1h).
- 정책은 Phase 3 TS(계열 단위, 가짜 보상) 하나뿐입니다. 비용 정규화와 할인은 없습니다.

## 27점 고정 격자의 한계 분석
`strategy/templates.py`의 GRID는 trend 6, mean_reversion 6, momentum 6, funding_basis 4, volatility 5로 **합계 27점**입니다.
1. **소진이 빠릅니다.** 계열당 4~6점이라 몇 사이클이면 모두 소진됩니다(r2에서 재확인).
2. **해상도가 거칩니다.** 파라미터마다 2~3개 값만 있고 직교 격자가 아닙니다. 국소 최적이나 민감도(평탄성)를 판단할 수 없습니다.
3. **구조 축이 고정돼 있습니다.** 신호 변환, 진입 규칙, 사이징, 유니버스 크기, 청산이 템플릿에 박혀 있어 MAP-Elites 행동 기술자가 거의 변하지 않습니다.
4. **N_eff가 무의미합니다.** 27개는 소수 군집이라 ONC가 2~5개 군집에 그칩니다.
5. **TS 학습 신호가 부족합니다.** 팔당 표본 4~6개입니다.
6. **S1 대리 지표가 없습니다.** 모든 점이 바로 S2로 갑니다.
7. **장점:** 사전 등록이라 다중성 회계가 명확합니다. 이 성질을 "사전 등록한 범위·문법 + 탐색 규칙"으로 옮깁니다.

결론: **파라미터는 사전 등록한 이산 범위 + 구조 축**으로 바꾸고, S1에서 대부분을 걸러 S2 예산을 지킵니다. 권장안 B(범위·구조 생성기, S1 IC 필터, TS 비용 정규화·할인, Random 대 TPE, MAP-Elites). Optuna가 거절되면 C(Random + TS)로 내려갑니다.

## 변경 범위
- `src/quant_research/*` (신규 `strategy/space.py`, 신규 패키지 `search/`, `research/`, `director/`, `backtest/` 수정)
- `tests/*`
- `tools/*`
- `docs/*`
- `CLAUDE.md`
- `README.md`
- `pyproject.toml` (Optuna·scipy는 별도 승인 후에만)
- `uv.lock`

## 순서와 단계별 검증 기준 (각 단계: 테스트 먼저 → 구현 → 게이트 → 계획 체크 → 커밋, 푸시는 승인 후)
- [x] 1. **공간 정의와 계산.** `strategy/space.py`
  - 축 타입: `IntAxis(lo, hi, step)`, `FloatAxis(lo, hi, step)`(10자리 반올림으로 값 고정), `ChoiceAxis(values)`. 잘못된 축은 생성 시 `ValueError`.
  - 계열별 파라미터 범위(사전 등록 초깃값): trend fast 6~96/6, slow 48~672/24(fast<slow); mean_reversion window 12~336/12, threshold 1.0~3.0/0.25; momentum lookback 24~720/24, quantile 0.60~0.95/0.05; funding_basis window 10~180/10, quantile 0.60~0.95/0.05; volatility short 6~96/6, long 96~720/24(short<long), ratio 0.40~1.00/0.05.
  - 구조 축: `side`(long_short / long_only; volatility는 원래 long만이라 제외), `smooth`(신호 EMA 0/3/6/12), `universe_n`(3/5/8), `sizing`(vol_target:48/96/168/336, equal), `time_stop`(0/24/72/168봉).
  - `templates.build`를 재사용하고 구조 축은 spec 필드 교체로 적용한 뒤 `StrategySpec`으로 다시 검증합니다.
  - `Space.size()`는 제약을 통과한 파라미터 조합 수 × 구조 조합 수, `nominal_size()`는 제약 전 곱입니다.
  - `generate(space, seed, count)`: numpy `default_rng(seed)`로 축마다 균등 표본. 제약 위반이나 S0 위반은 버리지 않고 사유와 함께 거부 후보로 돌려줍니다(단계 2에서 S0 trial로 기록).
  - 검증: 손으로 센 작은 공간(=12)과 일치, 같은 시드 = 같은 결과, 생성 후보 100%가 S0 통과 또는 사유 있는 거부, 축 밖 점 거부, 각 계열 공간 > 1,000.
- [x] 2. **S0 정적 검사 단계화.**
  - `strategy/s0.py`를 파이프라인 단계로 연결합니다. 정규형 해시로 중복을 제거하고, 지표 계층(생성 수, 중복 제거 후 수)을 기록합니다.
  - 검증: 중복이나 거부도 trial(stage S0)로 1건씩 기록됩니다. 회계 불변식 테스트를 통과해야 합니다.
- [x] 3. **S1 IC 표(ADR-022 조건부 승인 정의 그대로).**
  - 라벨: `decision_time + latency` 이후 다음 봉 시가 기준 h봉 수익률. 비용·펀딩 포함판을 따로 둡니다.
  - IC: 횡단면 Spearman. 유의성은 HAC(Newey-West) t 또는 겹치지 않는 표본.
  - 결과는 `feature_eval`에 기록합니다. 계열별 대리 지표(이벤트형은 이벤트 조건 수익률).
  - 검증: 합성 데이터에서 알려진 IC 재현(±0.02), 미래 정보 주입 누수 테스트, HAC t가 기준식과 일치(작은 고정 사례, statsmodels 없이), 평가 1회당 trial 1건.
- [x] 4. **비용 모델 `cost_s ≈ a + b·rows·ops`와 피크 메모리 모델.**
  - 단계별 실측(S1, S2, S3)으로 최소제곱 적합. 배치 전에 예측하고 예산을 넘으면 표본(심볼, 기간)을 줄입니다.
  - 검증: 보류 배치 90% 이상에서 예측/실측 0.5~1.5. 메모리 예측 초과 시 실행 전 거부.
- [x] 5. **충실도 편향 관리.** S1 탈락 후보의 5% 무작위 승격(시드 기록), 계열별 S1 대 S3 순위상관 기록.
  - 검증: 승격 비율이 5% ± 이항 95% 구간, 승격 시도에 `promoted_random` 표시와 회계 포함.
- [ ] 6. **탐색기: Random 기준선과 TPE.** Optuna 사용 여부를 **이 단계에서 확인받습니다**. 승인되면 버전 고정, 내부 평가마다 trial 기록. 같은 `cost_s` 예산으로 회수율, FWER, N_eff 증가량 비교.
  - 검증: Optuna 내부 평가 수 = trial 수, 심어 둔 합성 신호 회수율을 두 방법 모두 보고.
- [ ] 7. **N_eff (ONC 하나만).** S2 이상 `pnl_daily` 상관 → ONC → N_eff. DSR을 N_eff 기준과 N_total(S1 포함) 기준으로 두 번 보고하고 보수적인 쪽으로 판단. `min_sr(N_eff, T_split, q, skew, kurt, ρ)`와 참조 표, SE는 Mertens 식. scipy가 필요하면 이 단계에서 확인받습니다.
  - 검증: 블록 구조 합성 상관에서 군집 수 정확 복원, MC로 E[max]와 95% 분위 ±1% 재현, 기간 단위 통일(연환산 혼용 테스트).
- [ ] 8. **계열별 Thompson Sampling(할인·비용 정규화).** 보상 = 선별 구간 안 별도 검증 분할 성과(DSR 보정) / 실측 `cost_s`. 할인 또는 슬라이딩 창, 명시적 사전분포, 계열별 최소 예산.
  - 검증: 고정 보상 시뮬레이션 수렴, 보상 전환 시 할인 TS가 더 빨리 적응, 비용 2배 팔은 보상 절반.
- [ ] 9. **MAP-Elites 아카이브.** 행동 기술자는 회전율, 평균 보유기간, 순노출(사전 등록). elite 수축 추정, 신호 상관 상한.
  - 검증: 채움률·다양성 기록, 같은 셀 교체 규칙 결정적, 수축 후 순위가 표본 수에 따라 올바른 방향(합성).
- [ ] 10. **Director 통합과 실측 실행.** Director는 계열과 구조를 고르고 숫자는 탐색기가 정합니다(§7). 실데이터 소규모 배치(계열당 200 S1 → 상위 + 5% → S2 → S3)로 완료 기준 2개를 측정합니다. 연구 작업자는 BelowNormal, 스레드 상한, 동시 1개. 벌크·수집기 하트비트 지연 시 중단.
  - 검증: 완료 기준 2개 측정표, 수집기 gap·하트비트가 실행 전후 동일.
- [ ] 11. **문서.** progress, ADR(공간, S1, TS, ONC, MAP-Elites), failures, operations, test-results, measurements 갱신.

## 위험
- **다중성 폭증:** S1 시도가 수만 건이 될 수 있어 N_total 기준 DSR을 함께 보고합니다.
- **S1 편향:** 5% 무작위 승격과 순위상관으로 측정합니다.
- **자원:** RAM 16GB에서 수집기·벌크와 공존. 동시 작업자 1개, 메모리 예측으로 거부.
- **비용 모델 미보정(ADR-021):** 모든 S2/S3 수치는 "보정 전" 표시.
- **데이터 범위:** 8심볼, 6개월. T_split이 짧아 `min_sr`가 높습니다(검정력 낮음을 그대로 보고).
- **LLM 사용:** 탐색은 LLM 없이 돕니다. Director LLM은 구독 CLI 경로만, 한도 도달 시 PAUSED.
- **공간 초깃값:** 범위·간격은 사전 등록 초깃값이며 ADR로 남기고, 실측(소진 속도, S1 통과율)으로만 바꿉니다.

## 승인이 필요한 항목
- 이 계획 전체(구현 착수) — 이번 승인
- 새 의존성 Optuna(단계 6), scipy(단계 7, 필요 시) — 각 단계에서 별도 확인
- 작업자 2개로 확대(단계 10 실측 후)
- `git push`(현재 미푸시 커밋 3개: 4ab59fe, 94c5c5f, 2ec62d9)

## 검증 (전체)
- 단계마다 선언 게이트 `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q --cov`를 그대로 실행하고, 건드린 `tools/*.py`는 `uv run pyright tools/<file>.py`로 따로 확인합니다.
- 단계 10에서 실데이터 배치로 완료 기준 2개를 측정표로 보고합니다. CI는 푸시 승인 후 run id로 확인합니다.

## 진행 기록
- 2026-11-10 계획 승인(사용자 "승인", ExitPlanMode). 단계 1 착수.
- 2026-11-10 단계 1: `strategy/space.py`, 테스트 19개. 공간 크기(제약 후): trend 200,160 · mean_reversion 120,960 · momentum 115,200 · funding_basis 69,120 · volatility 1,344,720(합계 약 185만, 고정 격자 27점). 시드 3·2,000개 표본에서 S0 통과율: trend 96.0%·volatility 99.7%(거부는 모두 제약 위반), 나머지 100% — 계획대로. 구조 축 `sizing=equal`에서도 `vol_window`는 168로 고정해 의미 없는 중복을 막았습니다.
- 2026-11-10 단계 2: `search/s0_stage.py`(`run_s0`), 테스트 6개. 후보 1개 = S0 trial 1건(배치당 트랜잭션 1개). 통과=done, 제약·S0 위반=rejected(사유 전부), 정규형 중복=rejected(`duplicate: ... trial N`, 배치 안·이전 배치 모두). 제약 위반 후보는 spec이 없어 `spec_hash="point:"+sha256(point)`로 실험을 등록합니다 — 계획에 없던 세부, 회계 불변식 유지를 위해 추가.
- 2026-11-10 단계 3: `search/s1.py`(순수: 라벨, Spearman IC, NW HAC t, 이벤트 조건 수익률, `IcFeatureEvaluator`), `search/s1_stage.py`(`run_s1`: S1 trial 1건 + feature_eval 1행), 테스트 17개. 라벨은 `exec_open[k+h]/exec_open[k]-1`(결정 후 첫 체결가). 합성 IC(ρ=0.3 → 기대 0.2876) ±0.02 재현, 랜덤워크 과거 특징 |IC|<0.03, 누수 라벨은 IC>0.9로 탐지, HAC t는 교과서 반복문 구현과 1e-12 일치. 계획과의 차이: (1) 비용 포함판은 이벤트 수익률에서 포지션당 왕복 비용 1회를 뺀 `event_mean_net`으로 정의(횡단면 순위 IC는 상수 비용에 불변이라 IC 자체의 비용판은 의미 없음), (2) 펀딩 포함판은 펀딩 데이터 미적재(OPS-006)라 미구현이며 모든 S1 결과에 `funding_not_modelled` 표시, (3) S2 승격 순위는 `event_hac_t`(순수익 기준) — ADR로 기록 예정.
- 2026-11-10 단계 4: `search/cost_model.py`(단계별 OLS `a + b·rows·ops`, 메모리 `c + d·rows`, 실행 전 메모리 거부, 시간 예산에 맞춘 행 수 축소, 0.5~1.5 대역 비율), 테스트 7개. 합성(로그정규 잡음 σ=0.2) 보류 배치 200개 중 90% 이상이 대역 안. S1 trial metrics에 `rows`, `ops`를 기록해 실측 적합 입력으로 씁니다. **실데이터 완료 기준 측정은 단계 10**입니다(이 단계의 합성 검증은 완료 판정이 아님).
- 2026-11-19 단계 5(2026-11-11 착수분을 이어서 마무리): `search/promotion.py`(순수: `select_for_s2` 상위 k + 나머지에서 시드 고정 `promote_rate` 무작위 승격, `rank_corr` Spearman, `Selection.kind`), `search/s2_stage.py`(`run_s2`: 승격 후보 1개 = S2 trial 1건, metrics에 `promotion`(top_k/promoted_random)·`promotion_seed`·`promotion_rate`·`s1_trial_id`·`s1_score` 태그; `fidelity_by_family`: spec_hash별 최신 S1·S3 done trial을 짝지어 계열별 순위상관), `backtest/run.py`의 `run_backtest(tags=...)`(trial metrics에 병합, 거부된 trial에도 남음). 테스트 9개(승격률 5% 이항 95% 구간, 시드 결정성, 회계 1:1, 거부 trial 태그, S3 재평가 시 최신 trial 짝). 계획과의 차이: 순위상관은 테이블에 저장하지 않고 `fidelity_by_family`가 trial에서 계산해 단계 10 측정표에 적습니다(저장 위치가 계획에 없었음). 사이에 F-017(WS 수집기 heartbeat) 수정이 끼어 커밋 b453600으로 먼저 나갔습니다.
