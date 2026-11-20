# B4 T7: 키·계정이 필요 없는 외부 무료 소스 조사 (2026-11-20, researcher 서브에이전트)

> 공식 문서와 직접 API 호출로 확인했다. **미확인**으로 표시한 항목은 확인하지 못한 것이다. 약관 해석은 법률 검토가 아니다(사용자 판단 항목).

| 소스 | 키·계정 | 약관(개인 연구·로컬 저장) | 깊이·단위 | 받는 방법 | 시점(PIT) |
|---|---|---|---|---|---|
| Coin Metrics Community | 불필요(확인) | CC BY-NC 4.0(GitHub coinmetrics/data README), 최신 약관 원문 미확인 | BTC 1d 가격·온체인 2010~; 파생(funding·OI)은 403 | `community-api.coinmetrics.io/v4/timeseries/asset-metrics`, GitHub csv | `time` 00:00Z, 공개 지연 미확인(`AssetEODCompletionTime` 후보) |
| Alternative.me Fear & Greed | 불필요 | 출처 표기 필수 | 일 단위 2018-02-01~ | `api.alternative.me/fng/?limit=0&format=json` | D 00:00 UTC 값이 D 00:00 무렵 공개(관측), 사후 수정 여부 미확인 |
| Deribit DVOL | 불필요(무인증은 IP 차단 위험) | 회원 약관 "personal use only"(스니펫) | BTC/ETH 1h·1d, 2021-03-24~ | `public/get_volatility_index_data` (≤1000개/호출) | ts = 캔들 시작(관측), [T, T+1h) 확정은 T+1h로 가정 |
| Bybit REST/아카이브 | 불필요(미국 IP 403) | API Terms 비상업 조항(스니펫) | USDT 무기한 2020-03-25~ | REST v5 kline·funding, 아카이브는 틱 체결만 | 캔들 시작·정산 시각 문서화 |
| OKX REST | 불필요(약관 9.4 명시) | 개인·비상업, 재배포 금지 | 캔들 1Dutc 2020-01~, funding 2022-03~ | REST v5 history-candles, market-data-history | `1Dutc` 필수(기본 1D는 UTC+8) |
| DefiLlama stablecoins | 불필요 | 약관 8조 복사·미러 금지 → 로컬 저장 회색지대 | 일 단위 2017-11~ | `stablecoins.llama.fi/stablecoincharts/all` | 스냅샷/집계 의미·백필 미문서화 |

## 결정
- 어댑터 후보(키 불필요, 로컬 저장 허용으로 해석): Coin Metrics Community, Fear & Greed, Deribit DVOL. 교차 거래소 피처는 Bybit·OKX(조건부).
- 보류: DefiLlama(약관상 복사 금지, PIT 불명) → 사용자 판단.
- 전 소스 공통: 공개 시각이 문서화되지 않은 일 단위 값은 보수적 lag(하루 + 여유)로 등록하고, 수집 시 실제 공개 시각을 기록한다.
