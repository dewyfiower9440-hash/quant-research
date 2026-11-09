# Phase 2 실데이터 스모크 (tools/run_backtest.py)

> 템플릿은 고정 파라미터이며 탐색하지 않았습니다. 기간이 짧고 비용 모델은 보정 전입니다(ADR-021).
> **이 결과는 전략 성과의 증거가 아닙니다.** 목적은 엔진 교차 일치, 시도 회계, 미모델 항목 확인입니다.

- 생성: 2026-11-09T17:52:02+00:00
- 심볼: ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT', 'LINKUSDT']
- 기간(연구 구간): ['2025-01', '2025-06'], 결정 시점 4344개
- 레이크 파일 112개, 패널 적재 3.7초
- 최대 private 메모리 763MB
- 시도 회계: {"evaluations_attempted": 20, "new_trials": 20, "new_backtest_runs": 20, "refused_before_running": 0}
- 엔진 교차(이벤트 대 벡터, 자산 곡선 최대 상대 차이): {"trend": 1.3620846976315288e-14, "mean_reversion": 2.5594525766192996e-15, "momentum": 6.584624872573283e-15, "funding_basis": 0.0, "volatility": 4.490161050043839e-15}

| 템플릿 | 엔진 | 비용 배수 | trial | 상태 | 총수익 | 최악 가정 | SR(일) | MDD | 거래 | 비용 | 펀딩 | 평균 보유(h) | 표시 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| trend | event | 1 | 1 | done | -0.0121 | -0.0121 | -0.007873 | 0.07815 | 20882 | 258.7 | 0 | 74.55 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| trend | event | 2 | 2 | done | -0.03732 | -0.03732 | -0.03093 | 0.08957 | 20882 | 510.8 | 0 | 74.55 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| trend | event | 3 | 3 | done | -0.0619 | -0.0619 | -0.05379 | 0.1056 | 20882 | 756.5 | 0 | 74.55 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| trend | vector | 1 | 4 | done | -0.0121 | -0.0121 | -0.007873 | 0.07815 | 20882 | 258.7 | 0 | 74.55 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| mean_reversion | event | 1 | 5 | done | -0.1335 | -0.1335 | -0.1885 | 0.1439 | 7025 | 860.1 | 0 | 4.689 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| mean_reversion | event | 2 | 6 | done | -0.2101 | -0.2101 | -0.3109 | 0.2161 | 7025 | 1645 | 0 | 4.689 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| mean_reversion | event | 3 | 7 | done | -0.28 | -0.28 | -0.4295 | 0.2836 | 7025 | 2360 | 0 | 4.689 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| mean_reversion | vector | 1 | 8 | done | -0.1335 | -0.1335 | -0.1885 | 0.1439 | 7025 | 860.1 | 0 | 4.689 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| momentum | event | 1 | 9 | done | -0.02145 | -0.02145 | -0.06513 | 0.03555 | 11046 | 547 | 0 | 13.95 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| momentum | event | 2 | 10 | done | -0.07392 | -0.07392 | -0.2229 | 0.07877 | 11046 | 1064 | 0 | 13.95 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| momentum | event | 3 | 11 | done | -0.1236 | -0.1236 | -0.3625 | 0.1247 | 11046 | 1554 | 0 | 13.95 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| momentum | vector | 1 | 12 | done | -0.02145 | -0.02145 | -0.06513 | 0.03555 | 11046 | 547 | 0 | 13.95 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| funding_basis | event | 1 | 13 | done | 0 | 0 | nan | 0 | 0 | 0 | 0 | nan | funding_not_modelled: no funding data loaded (OPS-003/006) |
| funding_basis | event | 2 | 14 | done | 0 | 0 | nan | 0 | 0 | 0 | 0 | nan | funding_not_modelled: no funding data loaded (OPS-003/006) |
| funding_basis | event | 3 | 15 | done | 0 | 0 | nan | 0 | 0 | 0 | 0 | nan | funding_not_modelled: no funding data loaded (OPS-003/006) |
| funding_basis | vector | 1 | 16 | done | 0 | 0 | nan | 0 | 0 | 0 | 0 | nan | funding_not_modelled: no funding data loaded (OPS-003/006) |
| volatility | event | 1 | 17 | done | -0.03341 | -0.03341 | -0.1328 | 0.03905 | 3249 | 288.4 | 0 | 6.754 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| volatility | event | 2 | 18 | done | -0.06137 | -0.06137 | -0.236 | 0.06604 | 3249 | 568.5 | 0 | 6.754 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| volatility | event | 3 | 19 | done | -0.08852 | -0.08852 | -0.3245 | 0.09276 | 3249 | 840.4 | 0 | 6.754 | funding_not_modelled: no funding data loaded (OPS-003/006) |
| volatility | vector | 1 | 20 | done | -0.03341 | -0.03341 | -0.1328 | 0.03905 | 3249 | 288.4 | 0 | 6.754 | funding_not_modelled: no funding data loaded (OPS-003/006) |
