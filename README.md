# 单站点道路噪声分档 API

一个 FastAPI + SQLite 的单站点服务：按日录入逐分钟整数分贝值，或直接录入带原始边界的
分档分钟数；按日期范围和候选边界查询，得到每日各档分钟数，可分页续取。

- 某候选边界若会切开范围内任一天非空的历史分档，则**丢弃该边界**，并回报该日与它的原档。
- 用**余下的边界**统计：档位是 `[lower, upper)`，等于边界的读数归**较高档**。
- **重复上报去重**：同一日期以相同来源、相同完整内容重复 PUT 时，直接返回原修订号，
  不重写日档（响应 `deduplicated: true`）；内容或来源有变化才原子替换并升修订。
- 查询一旦建立就**冻结**：边界、各日计数、每页日期全部落库。之后补报、更正、重启都不改变旧查询；
  凭 `query_id` 逐页续取，跨重启一致。
- 新查询每次读取当时的最新数据。

## 启动

```bash
docker compose up --build -d
# Swagger: http://127.0.0.1:8000/docs      ReDoc: /redoc
curl -s http://127.0.0.1:8000/health
```

数据保存在命名卷 `noise-data` 的 `/data/noise.db`（WAL 模式）。`docker compose down` 保留数据，
`docker compose down -v` 才会清空。

端口和站点名可通过环境变量覆盖：

```bash
API_PORT=9000 NOISE_STATION=ring-road-north docker compose up --build -d
```

## 数据模型

| 表 | 作用 |
| --- | --- |
| `day` | 每天一行：日期、修订号、来源（`minutes`/`bins`）、内容指纹 `fingerprint` |
| `reading` | 逐分钟读数日的 1440 行整数 |
| `bin_interval` | 分档日的原档（半开区间，`NULL` 表示无界）与分钟数 |
| `query_log` | 查询快照：请求/生效边界、被拒边界及证据、缺失日期、分页大小 |
| `query_day` | 快照中每日各档计数与当时的修订号 |
| `query_page` | 冻结的每页日期列表 |

所有写入都在一个 `BEGIN IMMEDIATE` 事务内完成，整天替换要么全成、要么全不成。

`fingerprint` 是“来源 + 完整内容”的 SHA-256（schema v2 新增，启动时自动迁移旧库）。
重复上报判定在写入事务内完成：指纹一致即原样返回，不一致才替换。v1 旧行没有指纹，
首次比较时从实际存储的 `reading`/`bin_interval` 现算并回填，因此旧库无需任何手工迁移。

## 接口

### 录入/查询日数据

```bash
# 逐分钟：必须恰好 1440 个整数，dB ∈ [-100, 300]
curl -X PUT http://127.0.0.1:8000/days/2024-06-01 \
  -H 'Content-Type: application/json' \
  -d '{"mode":"minutes","values":[...1440 ints...]}'

# 分档（显式原档；首档 lower=null，末档 upper=null，必须连续且合计 1440）
curl -X PUT http://127.0.0.1:8000/days/2024-06-02 \
  -H 'Content-Type: application/json' \
  -d '{"mode":"bins","intervals":[
        {"lower":null,"upper":55,"minutes":210},
        {"lower":55,"upper":65,"minutes":690},
        {"lower":65,"upper":null,"minutes":540}]}'

# 分档（紧凑写法：n 个边界 + n+1 个计数，等价于上面的原档）
curl -X PUT http://127.0.0.1:8000/days/2024-06-02 \
  -H 'Content-Type: application/json' \
  -d '{"mode":"bins","boundaries":[55,65],"counts":[210,690,540]}'
```

同一日期再次 PUT 的语义：

- **内容、来源都相同**（采集程序重复上报）：直接返回原修订号，响应 `deduplicated: true`，
  `revision` 与 `updated_at` 不变，日档一行不重写，下游不会把重复上报误当成更正。
  分档日的两种写法（显式 `intervals` 与 `boundaries`+`counts`）规范化后等价，同样去重。
- **内容或来源有变化**（真实更正）：整天原子替换，`revision` 加一，`deduplicated: false`
  （来源也可以换，例如先报分钟再更正为分档）。

`GET /days/{date}` 取回最新版本；`GET /days?start_date=&end_date=` 列出区间内已存日期。

### 建查询（边界审查 + 冻结）

```bash
curl -X PUT ...  # 录入若干天
curl -X POST http://127.0.0.1:8000/queries \
  -H 'Content-Type: application/json' \
  -d '{"start_date":"2023-01-01","end_date":"2025-12-31",
       "boundaries":[55,60,65,70],"page_size":31}'
```

返回（节选）：

```json
{
  "query_id": "q-…",
  "requested_boundaries": [55, 60, 65, 70],
  "effective_boundaries": [55, 65],
  "rejected": [
    {"boundary": 60, "reason": "would_split_original_bin", "severed_total": 1,
     "severed_truncated": false,
     "severed": [{"date": "2024-06-02",
                  "original_bin": {"lower": 55, "upper": 65, "minutes": 690}}]}
  ],
  "bins": [{"index":0,"lower":null,"upper":55,"label":"<55"}, …],
  "missing_dates": ["2023-01-01", …],
  "pages_total": 36, "days_total": 1095, "days_available": 2
}
```

审查规则：只有 `bins` 来源的日子会约束边界（分钟来源本身有全分辨率，任何边界都能精确计算）。
边界落在某个**非空**原档内部（`lower < b < upper`）即被拒；边界恰好等于原档边沿不算切开。
被拒明细最多列 50 条，`severed_total` 给出真实总数，`severed_truncated` 标记是否截断。

计数规则：档位是 `[lower, upper)`，因此读数 `v` 落入第 `bisect_right(boundaries, v)` 档——等于
边界归较高档。分档日因为不存在切割，每个非空原档完整落入唯一目标档。

### 逐页取数

```bash
curl "http://127.0.0.1:8000/queries/q-…/pages/1"
curl "http://127.0.0.1:8000/queries/q-…/pages/2"
curl "http://127.0.0.1:8000/queries/q-…/days/2024-06-01"   # 单日
curl "http://127.0.0.1:8000/queries/q-…"                    # 元数据 + 页索引
```

页码从 1 开始，超过 `pages_total` 返回 404 `page_not_found`。范围内缺失的日期仍占页内位置，
返回 `available: false`、`counts: null`——这样页边界与日期集合在任何时候都可复现。

### 冻结语义

- 旧 `query_id` 的响应只会返回建查询时的边界与计数；`revision` 字段标明当时钉住的是哪一版。
- 重复上报被去重后不产生新修订，因此新旧查询都不会看到“幽灵更正”。
- 出错处理不会写快照（整天替换也是单事务）。
- 所有查询状态都在 SQLite 中，服务重启后凭 `query_id` 继续翻页，结果逐字节一致。

### 并发与一致性

- 每个 PUT 的“比对指纹 + 写入”都在同一个 `BEGIN IMMEDIATE` 事务内，写请求彼此串行：
  重复上报与真实更正并发时，最终修订号与内容按提交先后确定，后到者基于先到者的结果判定。
- 任何一步失败都会回滚整个事务，不会留下半套日档（例如只有 `day` 行没有明细）。
- 读（查询、翻页）走 WAL 快照，不被写入阻塞。

### 错误格式

所有错误统一为：

```json
{"error": {"code": "bin_total_mismatch", "message": "…", "expected": 1440, "got": 1200}}
```

常见 `code`：`wrong_minute_count`、`invalid_level`、`bin_total_mismatch`、`bins_not_contiguous`、
`boundaries_not_ascending`、`invalid_range`、`range_too_long`、`day_not_found`、
`query_not_found`、`page_not_found`、`date_outside_query`、`day_missing_in_query`、
`invalid_request`（请求体/路径校验失败，附 `problems`）。

## 本地开发与自测

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
uvicorn app.main:app --port 8000          # NOISE_DB 默认 ./data/noise.db，可覆盖
python scripts/demo.py http://127.0.0.1:8000
```

`scripts/demo.py` 只用标准库，演示：分钟日 + 分档日 → 重复上报去重 → 边界审查 → 两页取数 →
更正一天 → 旧查询保持不变、新查询看到更正。

## Swagger 演示流程（重复上报 / 真实更正 / 重启 / 旧查询回查）

`docker compose up --build -d` 后打开 http://127.0.0.1:8000/docs ，按顺序操作：

1. **重复上报**：`PUT /days/2024-06-01`，body 用
   `{"mode":"bins","boundaries":[65],"counts":[600,840]}`，连点两次 Execute。
   第一次返回 `"revision": 1, "deduplicated": false`；第二次返回
   `"revision": 1, "deduplicated": true`，`updated_at` 不变——日档未被重写。
2. **真实更正**：同一日期改 body 为 `{"mode":"bins","boundaries":[65],"counts":[500,940]}`，
   返回 `"revision": 2, "deduplicated": false`。`GET /days/2024-06-01` 可见新内容、修订 2。
3. **建查询**：`POST /queries`，body
   `{"start_date":"2024-06-01","end_date":"2024-06-01","boundaries":[60,65],"page_size":31}`，
   记下 `query_id`；`GET /queries/{query_id}/pages/1` 看到钉住的是 `revision: 2`。
4. **重启**：`docker compose restart`，等 `/health` 恢复。
5. **旧查询回查**：再次 `GET /queries/{query_id}/pages/1`，计数与修订号与重启前逐字节一致；
   `GET /days/2024-06-01` 仍是修订 2（数据在命名卷 `noise-data` 中）。
6. 再发一次第 2 步的更正 body，返回 `deduplicated: true`——重启后指纹依然生效。

## 边界与限制

- 单站点：站点名只来自 `NOISE_STATION` 环境变量，库内不存多站。
- 日期范围上限 3660 天；候选边界上限 100 个；`page_size` 1–366。
- 分贝读数限制在 [-100, 300]，边界限制在 [-1000, 1000]（整数）。
- 查询快照会物化每日计数，长期大量历史查询会使 `query_day` 增长；没有做快照回收接口。
- 只支持“以天为单位”的一次性提交，不支持单分钟增量补丁；更正即整天替换。
