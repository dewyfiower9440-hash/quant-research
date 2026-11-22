# 데이터 tier 파일럿·투영 (v2.3 단계 11, 2026-11-22)

> 이 표는 읽기 전용 계산이다. `tools/run_data_queue.py`와 같은 규칙을 쓴다.
> - 투영 = 데이터셋 lake 바이트 ÷ catalog의 (심볼, 월) 파일 수 × 남은 (tier 심볼 수 × 월 수) × 1.25.
> - 상한 판정: extras 폴더 합계 + 투영 ≤ 200GB, 그리고 디스크 여유 − 투영 ≥ 50GB.
> - 이 표는 측정이고 성과 주장이 아니다.

- 계산 시점: extras 폴더 0.38GB, 디스크 여유 247.0GB. lake 전체(1분봉·mark·index·premium 포함)는 약 63GB(2026-11-20 기록).

| 데이터셋 | 종류 | top | 보유 (심볼, 월) | 보유 MB | 남은 부분 투영 GB | 판정 |
|---|---|---|---|---|---|---|
| binance.um.bulk.funding | fundingRate | 전체 | 10,342 | 30.1 | 0.16 | 상한 안. **완료**(data-queue `done`) |
| binance.um.bulk.metrics_5m | metrics | 100 | 905 | 326.6 | 3.43 | 상한 안. **전체 다운로드 진행 중**(100심볼 중 약 20번째, 심볼당 약 3~5분) |
| binance.spot.bulk.klines_1m | spotKlines | 200 | 2 | 3.9 | 41.15 | 상한 안(파일럿 2개는 2026-10의 BTC·SHIB여서 과대 투영) |
| binance.um.bulk.book_depth | bookDepth | 30 (2023-01~) | 2 | 24.0 | 20.62 | 상한 안 |
| binance.um.bulk.agg_flow_1m | aggTrades | 10 | 0 | 0 | — | 파일럿 없음. 큐가 이 tier에 오면 파일럿부터 돈다 |
| external (fng·coinmetrics·dvol) | fetch_external | — | — | — | 작음 | 파일럿 없이 전체 실행 |

- 투영 합계는 약 65GB다. 지금 lake(약 63GB)에 더하면 약 128GB이고 상한(200GB) 안이다. 그래서 상한 때문에 건너뛰는 tier는 없을 것으로 본다. 각 tier의 실제 판정은 큐가 그 tier의 파일럿을 돌릴 때 다시 내린다.
- 시간은 실측 기준 추정이다. metrics 전체는 약 8시간, bookDepth(일 파일)는 그와 비슷하거나 더 걸린다. aggTrades는 월 0.3~1GB zip × 10심볼 × 약 80개월이라 수일이 걸린다. 세션 백그라운드로는 끝까지 받을 수 없으므로 `register_data_queue_task.ps1` 등록이 필요하다(사용자 작업).
