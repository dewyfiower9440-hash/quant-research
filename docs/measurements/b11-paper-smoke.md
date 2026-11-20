# B11 모의매매 인프라 점검 (tools/paper_smoke.py)

> 실제 lake, 더미 전략(v1 trend, 동일 가중, top-5), 일회용 정책 키, 새 원장. 성과가 아닌 경로 점검.

| 항목 | 값 |
|---|---|
| ledger | "C:\\dev\\qr-data\\paper\\smoke-20261120T140826\\ledger.db" |
| window | ["2026-11-20T00:00:00+00:00", "2026-11-20T12:00:00+00:00"] |
| ticks | 13 |
| submitted | 28 |
| open_orders_left | 1 |
| positions | {"9b23150f:BNBUSDT": -3.4945180000000002, "9b23150f:BTCUSDT": -0.03231099999999999, "9b23150f:ETHUSDT": -1.1415500000000003, "9b23150f:SOLUSDT": -27.783566, "9b23150f:XRPUSDT": -2084.5275929999993} |
| reconcile | [] |
| hash_chain_ok | true |
| hash_chain_detail | "ok" |
| feed_files_read | 202 |
| feed_max_file_rows | 180000 |

## tick

- {"now": "2026-11-20T00:05:00+00:00", "quotes": 0, "settled": 0, "canceled": 0, "submitted": 5, "refused": 0, "bars": 68556, "stale": ["LINKUSDT"]}
- {"now": "2026-11-20T01:05:00+00:00", "quotes": 4495, "settled": 5, "canceled": 0, "submitted": 0, "refused": 0, "slippage_bps": [0.7732290905979253, 0.23360702947788273, 1.4481601267572237, 0.6822678583605721, 1.5585225206502518], "bars": 68976, "stale": ["LINKUSDT"]}
- {"now": "2026-11-20T02:05:00+00:00", "quotes": 0, "settled": 0, "canceled": 0, "submitted": 0, "refused": 0, "bars": 69396, "stale": ["LINKUSDT"]}
- {"now": "2026-11-20T03:05:00+00:00", "quotes": 0, "settled": 0, "canceled": 0, "submitted": 5, "refused": 0, "bars": 69816, "stale": ["LINKUSDT"]}
- {"now": "2026-11-20T04:05:00+00:00", "quotes": 4500, "settled": 5, "canceled": 0, "submitted": 4, "refused": 0, "slippage_bps": [0.08712850583317422, 0.2821431594390994, 0.7747778252846786, 2.0662580067498544, 0.5240266205545935], "bars": 70236, "stale": ["LINKUSDT"]}
- {"now": "2026-11-20T05:05:00+00:00", "quotes": 3600, "settled": 4, "canceled": 0, "submitted": 0, "refused": 0, "slippage_bps": [-0.1859636271318434, 0.028817198104457386, 2.0662580067498544, 0.5209690023431464], "bars": 70656, "stale": ["LINKUSDT"]}
- {"now": "2026-11-20T06:05:00+00:00", "quotes": 0, "settled": 0, "canceled": 0, "submitted": 4, "refused": 0, "bars": 71076, "stale": ["LINKUSDT"]}
- {"now": "2026-11-20T07:05:00+00:00", "quotes": 3600, "settled": 4, "canceled": 0, "submitted": 5, "refused": 0, "slippage_bps": [-0.6121181912796217, -0.9302829759024352, 0.4888555316881123, -0.689417442260662], "bars": 71496, "stale": ["LINKUSDT"]}
- {"now": "2026-11-20T08:05:00+00:00", "quotes": 4500, "settled": 5, "canceled": 0, "submitted": 0, "refused": 0, "slippage_bps": [0.08740112747446492, -0.4766817777478628, 0.028641887614284083, 0.6881838827328405, 0.517089818502565], "bars": 71916, "stale": ["LINKUSDT"]}
- {"now": "2026-11-20T09:05:00+00:00", "quotes": 0, "settled": 0, "canceled": 0, "submitted": 2, "refused": 0, "bars": 72336, "stale": ["LINKUSDT"]}
- {"now": "2026-11-20T10:05:00+00:00", "quotes": 1800, "settled": 2, "canceled": 0, "submitted": 1, "refused": 0, "slippage_bps": [0.028577388997601014, 1.5661707126075017], "bars": 72756, "stale": ["LINKUSDT"]}
- {"now": "2026-11-20T11:05:00+00:00", "quotes": 900, "settled": 1, "canceled": 0, "submitted": 1, "refused": 0, "slippage_bps": [0.5208604614823116], "bars": 73176, "stale": ["LINKUSDT"]}
- {"now": "2026-11-20T12:05:00+00:00", "quotes": 900, "settled": 1, "canceled": 0, "submitted": 1, "refused": 0, "slippage_bps": [0.6918978758728918], "bars": 73596, "stale": ["LINKUSDT"]}
