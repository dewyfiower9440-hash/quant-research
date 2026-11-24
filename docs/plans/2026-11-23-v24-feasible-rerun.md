---
status: in-progress
task: v24-feasible-rerun
---

# v2.4: 실행 가능한 탐색 공간으로 재실행 + 신호 결합(M0)·Ridge(M1-R) 셀

## 목표
사용자 설계안 「설계 v2.3 (v2.2 개정판)」(2026-11-23)을 코드·데이터와 대조해 고친 범위로 구현한다. 기존 판정이 가진 두 결함(펀딩 비용 없이 계산됨, 실행 불가능한 lookback이 섞인 공간)을 없앤 조건으로 기존 격자를 다시 돌리고, 계열 신호를 결합하는 셀(선형 결합, Ridge)을 추가한다.
비목표: 판정 규칙(ADR-042)·α/k·봉인 창·S3 기준선 함수 변경, 누적 N 초기화, LightGBM과 그 밖의 새 패키지, 딥러닝, 미구현 계열(BTC 선후행·계절성) 구현, 비용 모형 보정, 실거래.

## 승인 근거
- 사용자 결정(2026-11-23, 설계안 검토 뒤 질문 4개):
  1. 캠페인 예약 작업은 새 공간이 들어갈 때까지 일시 정지한다(이미 `qr-campaign` Disabled, 데이터 큐·validator는 계속).
  2. 기존 격자(4h·1d × 상위 100 × w1·w2)를 재실행한다. 설계안 §3.4의 소급 진단은 하지 않는다. 결합 확인은 이미 구현된 ADR-049 규칙(셀의 최선 spec을 다른 창 확인 구간에서 1회 측정)을 그대로 쓴다.
  3. lookback 상한은 고정 90일이다.
  4. 모델 범위는 M0(선형 결합) + Ridge까지다. LightGBM은 다음 계획으로 미룬다.
- **이 계획을 승인하면 단계별 `git push`를 허용한 것으로 기록한다**(v2.3과 같은 방식, CI run id는 `docs/test-results.md`). 승인 메모로 거부하면 커밋까지만 한다.
- 멈추는 조건: 캠페인 예약 작업 다시 켜기(사용자 실행), 검증기 제출 전 확인, lake 상한 상향, 새 패키지, 실거래.

## 변경 범위
- `src/quant_research/**`
- `tests/**`
- `tools/**`
- `docs/**`
- `CLAUDE.md`
- `README.md`

## 데이터 변경 범위
- `C:/dev/qr-data/run/**` (새 캠페인 상태 `campaign-c2.json`·보고서; c1 파일은 읽기만)
- `C:/dev/qr-data/cache/**`, `C:/dev/qr-data/logs/**`
- `C:/dev/qr-data/meta/research.db` (trial 추가만; 기존 행 수정·삭제 없음)
- `C:/dev/auto/working/**` (세션 보고서)
- 건드리지 않음: `C:/dev/qr-data/raw`, `C:/dev/qr-data/lake`, `C:/dev/qr-data/meta/catalog.db`, `validator.db`, `C:/Project`

## Context (실측, 2026-11-23)
설계안의 전제 중 코드와 다른 것, 맞는 것을 먼저 고정한다.

**설계안과 다른 사실**
- 펀딩 손익은 두 엔진에 이미 구현돼 있다(`backtest/engines.py:137,260`, `backtest/costs.py:94 funding_payment`). c1 당시 펀딩 **데이터**가 없어 0으로 계산됐다: c1의 S2 trial 1,068건 중 979건에 `funding_not_modelled` 표시(`backtest/run.py:255`). 펀딩 tier는 지금 `done`이고 기본 패널 데이터셋에 들어 있다(`backtest/panel_store.py:47`). → 엔진 수정이 아니라 재실행이 필요하다.
- 결합 확인은 구현돼 있다(`search/campaign.py:114-118`, `campaign_runner.py`의 `cross_confirm`, 커밋 13f420c). c1의 100배치가 그 전에 돌았고 은퇴한 셀은 다시 돌지 않아 결합 판정이 0건이다. 현재 후보 규칙은 단일 창 기준을 쓰지 않는다.
- 예약 작업 3개는 2026-11-23 14:58에 등록됐다. `qr-campaign`은 사용자 결정으로 Disabled.

**설계안이 맞는 것**
- 78 대 59: S3는 선택 구간 SR 부호와 무관하게 최선 1개에 대해 돌고, 라벨은 앞 단계 사유로 붙는다(`campaign.py:103-118`). S3 78건 = `unstable` 59 + `no_signal` 17 + `cost_dominated` 2. 확인 SR 양수 18건 = `unstable` 14 + `no_signal` 4.
- lookback: w2는 앞이 봉인 구간이라 선택 구간의 워밍업이 첫 달뿐이다(`campaign_runner.py:245 split_window`, 일봉 약 31개). 공간의 창 길이는 주기와 무관하게 최대 720봉이다(`strategy/space.py:160-183`, v2 공간이 v1 축을 그대로 씀). 일봉 720봉은 w2 블록(19개월)보다 길다. S0에 실행 가능성 검사가 없다. breakout 33건 전부와 1d `sparse` 다수가 이 영향이다(기여 비율은 단계 2에서 측정).
- v2.2 §5.3 제목 "계열 11개"와 목록 15개 불일치.

**누적 N의 비용(추정, 기존 기준선 함수의 근사)**
| 누적 N(두 확인 구간) | 결합 기준(연 SR) | 참 연 SR 2.0 검정력 |
|---|---|---|
| 78 (현재) | 약 2.1 | 약 0.45 |
| 300 | 약 2.5 | 약 0.28 |
| 600 | 약 2.7 | 약 0.2 |
이 계획의 재실행과 새 셀은 N을 늘린다. 기준은 낮추지 않는다. 라운드마다 실제 표를 기록한다(단계 3).

## 가정
- 재실행은 새 run id `c2`로 한다. c1 상태 파일은 그대로 두고, c1의 S3 판정 수는 `prior_s3`로 c2 기준에 더해진다(`campaign_runner.earlier_histories`, ADR-049 결정 2).
- 공간이 바뀌므로 새 공간 버전(v3)의 사전 등록 해시를 첫 실행 전에 기록한다. 이미 평가한 trial은 지우지 않는다.
- 결합·Ridge 셀도 기존 깔때기를 쓴다: 셀의 설정들을 선택 구간 검증 분할(마지막 25%, 기존 `pipeline` 분할)에서 비교해 최선 1개만 S3로 보낸다. 그래서 셀 배치당 N은 자기 확인 1 + 교차 확인 1이다(설계안의 "셀당 9"보다 적다).
- 결합·Ridge의 가중치는 숫자 상수로 spec에 들어간다. 그래서 검증기는 기존 경로 그대로 그 spec을 봉인 구간에서 잰다(모델 산출물 파일 없음).
- 국면 축은 사전 등록만 하고 마지막 라운드에서 연다.

## 선택한 접근
**A. 기존 캠페인 구조에 공간 v3·새 셀 종류를 더하고 새 run으로 재실행(채택).** 라벨·공간·S0 검사는 순수 함수로 고치고, 결합·Ridge는 DSL spec으로 표현해 `TrialLedger.evaluate` 경로와 검증기를 그대로 쓴다.
- 결정 이유: trial 회계 불변식, 결합 확인, 검증기 경로가 이미 테스트돼 있다. 새 모듈은 가중치를 만드는 학습기뿐이다.
- B. 설계안 그대로(엔진 재작성, 양방향 교차, 소급 진단, LightGBM): 이미 있는 것을 다시 만들고 N을 더 쓴다. 검증기 재현 경로가 없다. 기각.
- C. c1을 이어서 은퇴 셀만 다시 열기: 공간이 바뀌어 재개 설정 검사와 맞지 않고, 옛 공간과 새 공간의 결과가 한 파일에 섞인다. 기각.

## 단계
- [x] 1. **설계 문서와 사전 등록.** 사용자 설계안을 위 Context의 정정과 네 가지 결정을 반영해 `docs/plans/2026-11-23-autonomous-quant-research-v2.3.md`로 저장한다(상태 `design`, 정정 내역을 머리말에 명시). ADR-050(라벨 v3), ADR-051(lookback 상한 90일·`infeasible`), ADR-052(펀딩 없는 선물 백테스트는 data_missing), ADR-053(결합 셀 M0), ADR-054(Ridge 셀 M1-R), ADR-055(국면 게이트 축·라운드 순서). v2.2 §5.3 제목을 "계열 15개"로 고치고 구현 대응표를 붙인다. 부록 A(기준 매개변수 = 상한 적용 뒤 각 범위의 중간값, M0 포트폴리오 구성, Ridge α 3개, 특징 목록, 확인 구간은 재학습 없이 고정)를 값으로 채운다. — 파일: `docs/plans/`, `docs/decisions.md` — 테스트: 없음(문서) — 검증: 게이트
- [x] 2. **라벨 v3와 재집계.** `classify`에 `below_bar`(확인 SR > 0이지만 결합 확인 미충족, 하위 사유 `sign_mismatch`/`pooled_below`/`no_cross`)와 `infeasible`을 더한다. `unstable`은 "확인 SR ≤ 0"으로 문서와 구현을 맞춘다. 앞 단계 사유(`no_signal`·`cost_dominated`)가 붙은 배치도 S3 판정 여부를 따로 기록한다. 읽기 전용 도구 `tools/relabel_campaign.py`가 c1 103배치를 v3로 다시 분류한 교차표를 쓴다(계산 재실행 없음, trial 증가 0). `sparse` 가운데 상한 초과 lookback 비율도 낸다. 78/59 원인을 `docs/failures.md`에 기록한다. — 파일: `src/quant_research/search/campaign.py`, `tools/relabel_campaign.py`, `docs/measurements/v24-relabel.md` — 테스트: `tests/search/test_campaign.py`(경계값 SR = 0, 노출 5%, 교차 없음; 합계 = 배치 수 불변식; 옛 상태 JSON 호환) — 검증: 게이트, `uv run pyright tools/relabel_campaign.py`
- [x] 3. **S3 검정력 표.** `s3_power(days, n, true_sr_ann)`(순수 함수, 기존 `expected_max_sr` 재사용)와, 라운드 시작 때 그 시점 누적 N으로 표를 보고서에 쓰는 연결. 표는 보고용이고 기준선 함수는 바꾸지 않는다. — 파일: `src/quant_research/search/multiplicity.py` 또는 `campaign_runner.py`, `tools/run_campaign.py` — 테스트: 알려진 값 대조, N 증가 시 검정력 단조 감소 — 검증: 게이트
- [x] 4. **실행 가능한 공간 v3.** 주기별 상한: 창 연산은 90일 ÷ 봉 길이(1h 2,160 / 4h 540 / 1d 90봉), EMA는 span × 3 ≤ 90일. `strategy/ops.lookback`으로 spec의 필요 워밍업을 계산해 블록의 워밍업(90일)을 넘으면 S0에서 `infeasible`로 거부한다(예산·N 미차감). `split_window`의 워밍업을 "첫 달"에서 "첫 90일(월 경계로 올림)"로 바꾸고, 선택 ≥ 9개월·확인 ≥ 4개월을 검사한다. 공간 v3 사전 등록 해시 기록. — 파일: `src/quant_research/strategy/space.py`, `strategy/s0.py`, `search/campaign_runner.py`, `search/campaign.py` — 테스트: 상한 위반 spec 거부, 모든 결정의 워밍업 행이 같은 연구 블록 안(속성 테스트), 공간 크기 전후 표, 기존 v1/v2 조회 경로 불변 — 검증: 게이트
- [x] 5. **펀딩 없는 선물 판정 금지.** 캠페인 경로에서 USD-M 선물 백테스트가 `funding_not_modelled`이면 그 셀을 `data_missing`(funding)으로 처리한다(조용히 0으로 두지 않는다). 펀딩 손익의 손계산 사례 3개(롱 1회 정산, 숏 1회 정산, 주기 변경 구간)를 두 엔진에 대해 테스트로 고정하고, 없는 것만 추가한다. 손익 분해(가격·수수료·스프레드·충격·펀딩)를 캠페인 보고서에 넣는다. — 파일: `src/quant_research/search/pipeline.py`, `backtest/run.py`(필요 시), `tests/backtest/` — 테스트: 위 사례, 펀딩 행이 없으면 data_missing — 검증: 게이트
- [x] 6. **결합 셀 M0.** 셀 계열 자리에 `combo`를 둔다. 구성 신호는 부록 A의 기준 매개변수로 만든 횡단면 계열 신호(`zscore_xs`)이고, 그 셀의 데이터로 계산할 수 있는 계열만 쓴다. 설정 2개: `lin_eq`(동일 가중), `lin_ic`(선택 구간 앞 75%에서 추정한 신호별 IC, HAC t로 축소, 음수는 0). 가중치는 숫자로 spec에 들어간다. 포트폴리오 구성은 고정 1개(분위 0.8, 변동성 목표, 리밸런스 24h, 밴드 2%). S2·S3에서 고른 최선 매개변수는 입력으로 쓰지 않는다. 진단(구성 신호 상관행렬, 단일 계열 최선과의 차이)은 보고만 한다. — 파일: `src/quant_research/strategy/combo.py`(신규), `strategy/templates.py`, `strategy/space.py`, `search/pipeline.py` — 테스트: 독립인 약한 신호 k개를 심으면 결합 SR이 √k에 가깝게 오름, 미래 값이 섞인 구성 신호는 누수 테스트에서 실패, 가중치가 확인 구간 데이터에 의존하지 않음 — 검증: 게이트
- [x] 7. **Ridge 셀 M1-R.** 특징 = M0 구성 신호 + 고정 보조 특징(최근 수익률 1·5·20봉, 실현 변동성 20봉, 거래대금 순위, 프리미엄 z점수; 모두 상한 안). 라벨 = 다음 24h 비용·펀딩 차감 후 수익률의 횡단면 순위. 선택 구간 앞 75%에서 purge(라벨 구간)·embargo(max(특징 lookback, 24h))를 두고 numpy로 적합한다. 설정 3개(α = 1, 10, 100). 계수는 숫자로 spec에 들어가고 확인 구간에서 다시 학습하지 않는다. 결측 행은 학습에서 뺀다. — 파일: `src/quant_research/model/`(신규: 특징 패널, ridge, purge·embargo), `strategy/combo.py` — 테스트: purge·embargo 경계, 미래 값 특징 누수 테스트, 같은 입력에서 계수 재현, 라벨을 블록 단위로 섞으면 확인 SR 분포가 0 근처(과적합 탐침) — 검증: 게이트
- [ ] 8. **결합 단계의 거짓양성·검정력 측정.** 기존 귀무 하네스(`validator/null_pipeline.py`의 블록 순열·부호 뒤집기)를 재사용해 (선택 → S3 → 교차 → 결합 판정) 단계와 M0·M1-R 전체의 거짓양성률을 잰다. 축소 예산으로 200회 이상, Wilson 95% 구간을 보고한다. 심은 선형 알파(오라클 z = 2·4·6)의 회수율을 단일 계열·M0·Ridge로 비교한다. 포그라운드 20분 단위로 이어 돌린다. — 파일: `tools/measure_combined_null.py`(신규), `docs/measurements/v24-combined-null.md` — 테스트: 도구의 순수 부분 단위 테스트 — 검증: 측정표(점추정 ≤ 0.05가 아니면 그대로 보고하고 9단계 전에 멈춘다)
- [ ] 9. **국면 게이트 축(사전 등록, 마지막 라운드).** BTC 기준 확장 창 분위(`expanding_quantile` 재사용): 추세 국면(90일 수익률), 변동성 국면(30일 실현 변동성). 축 `regime_gate ∈ {none, trend_up, trend_not_down, vol_low, vol_not_high}`. 기준 자산 값을 모든 종목 행에 붙이는 패널 열이 필요하면 그 범위만 더한다. — 파일: `src/quant_research/strategy/space.py`, `strategy/templates.py`, `backtest/panel.py`(필요 시) — 테스트: 국면이 맞지 않으면 포지션 0, 미래 값 누수 없음 — 검증: 게이트
- [ ] 10. **캠페인 c2 시동.** 라운드 순서: G1′(w2·4h/1d·top100) → G2′(w1) → M0 → M1-R → D(새 데이터 계열, tier가 `done`일 때) → G3(1h) → G4(top30) → 국면 축. `register_campaign_task.ps1`의 기본 run id를 c2로 바꾼다(Pester 갱신). 포그라운드로 첫 틱을 돌려 확인한다: 펀딩이 손익에 들어감, `infeasible` 거부, 결합 확인이 실제로 판정됨, 회계 누락 0, 펀딩·premium 셀에 커버리지 하한으로 인한 `data_missing` 없음(미확인 건 해소). 측정표 `docs/measurements/campaign-c2.md`. 이후 무인 실행은 사용자가 작업을 다시 켠 뒤 진행한다. — 파일: `tools/run_campaign.py`, `tools/register_campaign_task.ps1`, `tests/powershell/` — 검증: 측정표, 게이트
- [ ] 11. **최종 문서와 리뷰.** progress, test-results(CI run id), operations, CLAUDE.md, `C:/dev/auto/working/` 보고서. `code-reviewer` 전체 범위 리뷰(2·4·6·7단계 뒤에도 각각), CONFIRMED 지적은 테스트로 재현해 수정. — 검증: 게이트, CI 녹색

## 리스크와 대응
- **누적 N 증가로 기준 상승:** 의도된 비용이다. 라운드마다 검정력 표로 보고한다. 재실행은 한 번만 하고 소급 진단은 하지 않는다. 결과가 0이면 셀별 음성 결과가 산출물이다.
- **이미 본 S3 결과가 새 설계에 스며듦:** 기준 매개변수는 범위 중간값으로 기계적으로 정한다. lookback 상한은 블록 길이에서만 유도한다. Ridge 설정과 특징은 1단계에서 고정하고 해시를 기록한다.
- **확인 구간 단축:** w2는 워밍업 90일로 선택 약 11개월, 확인 약 5개월이 된다. 결합 확인이 두 확인 구간을 합쳐 상쇄한다. 표로 보고한다.
- **느린 신호 손실:** 일봉 90봉 초과 신호는 이번 범위에서 포기한다. 더 긴 블록이 생기면 다시 다룬다.
- **메모리:** 연구 프로세스는 하나, 패널은 한 번에 하나(기존 가드). Ridge 특징 패널은 4h·100종목·특징 약 20개로 수십 MB 수준(추정)이고 첫 실행에서 실측한다.
- **되돌리기:** `qr-campaign`은 꺼진 상태로 둔다. c2 상태 파일 삭제와 코드 revert로 되돌린다. trial은 추가만 하므로 research.db 행은 지우지 않는다.

## 영향 범위
- 상태 파일: `run/campaign-c2.json` 신규. 배치 요약에 v3 라벨 필드 추가(옛 JSON 읽기 호환).
- CLI: `tools/run_campaign.py`(라운드 순서, 검정력 표), 새 도구 2개(`relabel_campaign.py`, `measure_combined_null.py`), `register_campaign_task.ps1` 기본 run id.
- 탐색 공간 v3 해시(사전 등록 갱신). 검증기·봉인·판정 규칙은 변경 없음.
- 문서: 설계 v2.3(정정본), ADR-050~055, operations, progress, test-results, measurements.
- 마이그레이션: DB 스키마 변경 없음.

## 검증
- 매 커밋 전 `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q --cov`를 그대로(단독, 파이프 없이) → exit 0 → 커밋 → push → CI run id 기록.
- 건드린 `tools/*.py`는 `uv run pyright tools/<file>.py`, `*.ps1`은 PSScriptAnalyzer·Pester.
- 끝에서 끝: 10단계의 c2 첫 틱 측정표(펀딩 반영, `infeasible`, 결합 판정 실행, 회계 누락 0).

## 진행 기록
- 2026-11-23 계획 작성.
- 2026-11-23 단계 1: 설계 v2.3 정정본(docs/plans/2026-11-23-autonomous-quant-research-v2.3.md), ADR-050~055, v2.2 §5.3 제목 정정과 구현 대응표 — 계획대로. 기준 매개변수 표는 문서에 값으로 적었고 코드 대조 테스트는 단계 6에서 둔다.
- 2026-11-23 단계 2: `below_bar`(하위 사유 3개)·`infeasible` 라벨, `summary_from_json`, `relabel`, lookback 상한 판별(`window_cap_bars`·`ema_cap_bars`·`exceeds_cap`), 도구 `tools/relabel_campaign.py`, 재집계표 `docs/measurements/v24-relabel.md`, F-021 — 계획대로. 상한 판별 함수는 재집계에 필요해 단계 4에서 당겨 왔다. `infeasible`의 S0 거부는 단계 4.
- 2026-11-23 단계 3: `s3_power`·`s3_power_table`, `pooled_n`·`pooled_power`, `split_bounds`(패널 없이 분할 경계), 캠페인 보고서에 결합 기준·검정력 표와 교차·결합 열 — 계획대로. 실측: N 80 → 기준 연 SR 2.13, 참 SR 2의 통과 확률 0.44.
- 2026-11-23 단계 4: `SPACES_V3`(이름 `family@tf/v3`, 40개)·`INFEASIBLE_V3`(trend@1d, volatility@1d), S0 `warmup_bars`·`S0Context.max_warmup_bars`, `SplitRule`(v3: 워밍업 3개월, 선택 9·확인 4개월 이상), `CampaignConfig.space_version`, `infeasible` 배치(패널·trial 없이 기록), 도구 `--space-version`, 표 `docs/measurements/v24-space-v3.md` — 계획과 다른 점: v2 공간을 고치지 않고 v3를 별도 이름으로 등록했다(c1 점의 재구성 경로 유지). 공간 해시 대신 축 값을 테스트로 고정했다. 단계 3과 한 커밋.
- 2026-11-23 단계 5: v3 캠페인은 모든 셀에 펀딩 열(커버리지 80%)과 정산 이벤트를 요구한다(`missing_data(require_funding=)`, `missing_inputs(also=)`, 선택·확인·교차 패널 모두). `BatchSummary.best_funding_fraction`과 보고서의 순수익·비용·펀딩 열. 엔진 손계산 사례는 롱·숏 1회 정산이 이미 있어 '한 구간에 정산 2회'만 추가했다 — 계획과 다른 점: 판정 금지는 `funding_not_modelled` 표시를 사후에 보는 대신 배치 시작 전에 패널로 검사한다(trial을 쓰지 않는다). 손익 분해는 비용 합계와 펀딩까지다(수수료·스프레드·충격의 세부 분해는 S2 지표에 없다).
- 2026-11-24 단계 6: 결합 셀 M0. `templates.constituent`·`build_model`, 모델 공간 `MODEL_SPACES`(`combo@tf/v3`), `search/model_cells.py`(`fit_ics`·`ic_weights`·`combo_points`), `FixedSearcher`, 러너의 모델 셀 분기(fit 75% 추정, 검증 25%에서 비교), 모델 계열의 S0 복잡도 한도 — 계획과 다른 점: `strategy/combo.py`를 따로 두지 않고 템플릿과 `search/model_cells.py`에 나눠 넣었다. 구성 신호 상관행렬 진단은 구현하지 않았다(fit 통계 기록으로 대신). 세부는 ADR-053·054 보강.
- 2026-11-24 단계 7: Ridge 셀 M1-R. 보조 특징 6개(`AUX_TERMS`), `training_rows`(purge·엠바고·결측 제외·횡단면 표준화), `ridge_coefficients`(numpy), `ridge_points`(α 3개), 공간 `ridge@tf/v3` — 계획과 다른 점: `src/quant_research/model/` 패키지 대신 `search/model_cells.py` 한 파일이다. 과적합 탐침은 '라벨 섞기' 대신 '수익률과 무관한 특성' 패널로 했다. 단계 6과 한 커밋.
