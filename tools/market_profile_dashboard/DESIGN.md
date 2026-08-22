# Market Profile Dashboard 设计文档

## 1. 项目定位

这是一个独立的 MVP 项目，用于在局域网中浏览 `/srv/kline/canonical/test/tick` 下的简化 Tick 数据。

用户可以选择：

- 交易日；
- 具体合约；
- 查看原始 Tick 路径；
- 查看由 Tick 聚合出的 1 分钟 K 线；
- 查看基于 Volume 的 Market Profile；
- 查看由 VPOC、VAL、VAH 形成的上午价格 zone，以及下午首次触碰方向；
- 在价格图中叠加一条 session VWAP 曲线作为参考。

本项目只做历史数据浏览和研究，不做交易下单、策略回测、行情推送或账户管理。

## 2. 与旧项目的关系

旧项目的以下内容作为参考：

- 交易时段定义和左闭右开时间语义；
- 累计成交量差分；
- Tick 到 1 分钟 K 线的 OHLCV 聚合；
- VPOC、VAL、VAH 和价格 zone 的概念；
- ECharts 的图形组织方式；
- Fastify 的静态网站和受控路径设计。

本项目不直接复用旧项目的 pipeline，原因是目标数据格式不同：

```text
目标数据：update_date,symbol,last_price,volume
旧 Profile：TradingDay,InstrumentID,UpdateTime,UpdateMillisec,
            LastPrice,Volume,Turnover,OpenInterest
```

MVP 不实现旧项目的以下复杂逻辑：

- LTP/VWAP 双 Profile confluence；
- HVN/LVN 多尺度持久性匹配；
- 基于 Turnover 的 interval VWAP Profile；
- 跨品种代表合约选择；
- bounce/break/timeout 事件评价；
- 统计显著性、策略收益和回测。

## 3. 数据契约

### 3.1 输入目录

```text
/srv/kline/canonical/test/tick/
├── IC2607/
│   ├── 20260701.csv
│   └── 20260702.csv
├── IF2608/
├── T2609/
└── lh2607/
```

文件名中的日期是交易日，目录名是合约。服务启动时扫描两层目录，建立内存目录索引，不预先读取全部 Tick 行。

### 3.2 输入字段

```text
update_date,symbol,last_price,volume
```

字段语义：

| 字段 | 类型 | 语义 |
|---|---|---|
| `update_date` | datetime | 北京时间，格式 `YYYYMMDD HH:mm:ss.SSS` |
| `symbol` | string | 具体合约，例如 `IC2608` |
| `last_price` | number | 当前快照的最新价 |
| `volume` | number | 交易日累计成交量 |

`volume` 默认按累计值处理：

```text
delta_volume[t] = max(volume[t] - volume[t-1], 0)
```

如果发生回退，当前 Tick 的增量记为 0，并写入质量标记 `VOLUME_RESET`。服务不会静默把负值当成有效成交量。

### 3.3 数据校验

每个文件读取时检查：

- 日期格式和文件名日期一致；
- `symbol` 与目录名一致；
- 时间按升序排列；
- 价格为有限正数；
- 成交量为有限非负数；
- 未知字段允许存在，但核心字段必须存在。

质量统计至少包括：

```text
raw_ticks
valid_ticks
filtered_ticks
outside_session_ticks
backwards_ticks
volume_resets
zero_delta_ticks
first_tick_baseline
```

### 3.4 合约和交易时段配置

新项目维护自己的版本化配置文件，不在运行时依赖旧项目目录：

```text
config/contracts.json
```

第一版只覆盖当前数据中出现的合约根：

| 合约根 | Tick size | 交易时段 |
|---|---:|---|
| IF、IC、IH、IM | 0.2 | 09:30-11:30、13:00-15:00 |
| T、TF | 0.005 | 09:30-11:30、13:00-15:15 |
| TS | 0.002 | 09:30-11:30、13:00-15:15 |
| TL | 0.01 | 09:30-11:30、13:00-15:15 |
| LH | 5 | 09:00-10:15、10:30-11:30、13:30-15:00 |

合约根匹配大小写不敏感，原始合约名称保留用于显示。

所有时间段均为左闭右开 `[start, end)`。开盘前、午休、收盘后的快照保留在原始文件中，但不会进入 K 线和 Profile 计算。

## 4. 计算模型

### 4.1 标准化 Tick

标准化后的内部 Tick：

```text
{
  tradingDay,
  instrument,
  eventTime,
  millisOfDay,
  session: "morning" | "afternoon",
  lastPrice,
  cumulativeVolume,
  deltaVolume,
  qualityFlags
}
```

过滤顺序：

1. 解析时间和数值；
2. 丢弃无效价格；
3. 丢弃时间倒退的 Tick；
4. 丢弃非交易时段 Tick；
5. 计算累计成交量差分；
6. 给有效 Tick 分配交易时段。

第一笔有效 Tick 只建立成交量基准，其 `deltaVolume` 为 0。

### 4.2 1 分钟 K 线

每个 Tick 按北京时间向下取整到分钟：

```text
bar_time = floor(event_time, 1 minute)
```

同一分钟内：

```text
open  = 第一笔 last_price
high  = max(last_price)
low   = min(last_price)
close = 最后一笔 last_price
volume = sum(deltaVolume)
tickCount = Tick 数量
```

没有 Tick 的分钟不补空 K 线。休盘前的 K 线由下一根有效 Tick 或文件结束时封口。

### 4.3 VWAP 参考曲线

由于输入没有 `Turnover`，这里计算的是基于快照的 Volume-weighted price，不宣称是真实逐笔成交 VWAP。

主曲线为交易日累计 VWAP：

```text
sessionVwap[t] =
  sum(last_price[i] * deltaVolume[i]) /
  sum(deltaVolume[i])
```

曲线在每个有效 Tick 上更新，并在图上按时间连接。没有新增成交量的 Tick 沿用上一有效 VWAP。

页面应明确标注：

```text
VWAP: volume-weighted estimate from tick snapshots
```

MVP 只显示 `sessionVwap`，不单独显示分钟级 VWAP。

### 4.4 Volume Profile

Profile 默认只使用上午数据，保证下午 support/resistance 观察不产生 look-ahead：

```text
profileVolume[priceBin(last_price)] += deltaVolume
```

价格分桶：

```text
priceBin = round(last_price / tickSize)
displayPrice = priceBin * tickSize
```

第一版只保留一个 raw Volume Profile，不做 LTP/VWAP 双 Profile，也不做跨方法 confluence。

输出基础结构：

```text
price
volume
volumeShare
```

### 4.5 VPOC、Value Area

`VPOC` 是上午 Profile 中成交量最大的价格 bin。若多个相邻 bin 并列最高，合并为 plateau zone。

Value Area 默认覆盖 70% 上午成交量：

1. 从 VPOC 开始；
2. 比较左右相邻 bin 的成交量；
3. 优先加入成交量更大的一侧；
4. 直到累计成交量达到总量的 70%；
5. 左边界为 `VAL`，右边界为 `VAH`。

### 4.6 MVP zone

MVP 只保留最容易解释的三个结构：

```text
VPOC
VAL
VAH
```

VPOC、VAL 和 VAH 各自生成一个以 tick size 为宽度的 zone。相邻 zone 不做复杂的局部峰谷合并。

后续版本再考虑 HVN、LVN、profile edges 和平滑参数。

### 4.7 Support / Resistance 行为标签

Profile 只产生结构 zone，不直接声称每个 zone 都是支撑或阻力。

下午事件只观察上午冻结后的 zone：

```text
上一个价格 > zone.upper 且当前价格 <= zone.upper
    -> support touch

上一个价格 < zone.lower 且当前价格 >= zone.lower
    -> resistance touch
```

MVP 只记录每个 zone 的第一次触碰方向：

- 从上方进入：`support`；
- 从下方进入：`resistance`；
- 没有触碰：`neutral`。

MVP 不判断 bounce、break、timeout，也不计算 MFE/MAE。这样可以先验证 zone 和方向标签是否符合观察习惯。

## 5. 系统架构

```text
Browser
  |
  | HTTP/JSON
  v
Fastify + TypeScript
  |- CatalogIndexer
  |- MarketProfileEngine
  |- InMemoryCache
  |- Read-only API
  `- Static frontend
        |
        `- ECharts

/srv/kline/canonical/test/tick
  -> streaming CSV reader
  -> normalized ticks
  -> 1min bars / session VWAP / VPOC-VAL-VAH zones
```

新项目首版不依赖 Python，不把旧项目的复杂 pipeline 作为运行时依赖。这样可以做到：

- 只安装 Node 24 和 npm 依赖即可运行；
- HTTP 请求和计算使用同一套数据模型；
- 不需要生成旧项目要求的 CTP 兼容中间文件；
- 算法更容易针对四列输入测试。

## 6. 目录规划

```text
tools/market_profile_dashboard/
├── DESIGN.md
├── package.json
├── package-lock.json
├── tsconfig.json
├── config/
│   └── contracts.json
├── src/
│   ├── server.ts              # Fastify 启动入口
│   ├── config.ts              # 环境变量和配置校验
│   ├── catalog.ts             # 日期/合约文件索引
│   ├── csv.ts                 # 流式 CSV 读取
│   ├── types.ts               # 内部模型和 API 类型
│   ├── sessions.ts             # 交易时段和交易日校验
│   ├── engine.ts               # Tick、1min、VWAP、Profile 计算
│   ├── api.ts                  # HTTP 路由
│   └── paths.ts                # root containment 和路径安全
├── public/
│   ├── index.html
│   ├── app.js
│   ├── styles.css
│   └── vendor/
│       └── echarts.min.js
└── tests/
    ├── sessions.test.ts
    ├── engine.test.ts
    ├── catalog.test.ts
    └── api.test.ts
```

## 7. HTTP API

MVP 只提供一个图表数据接口，避免把同一份分析结果拆成多个请求。

### 7.1 健康检查

```text
GET /api/health
```

健康检查返回服务状态、算法版本和缓存条目数量，不返回任意文件路径。

### 7.2 目录接口

```text
GET /api/days
GET /api/days/:date/instruments
```

目录接口只返回索引信息：

```json
{
  "date": "20260701",
  "instrument": "IC2608",
  "root": "IC",
  "tickSize": 0.2,
  "fileSize": 1026763,
  "modifiedAt": "...",
  "analysisCached": false
}
```

### 7.3 图表数据接口

```text
GET /api/days/:date/instruments/:instrument/chart
```

chart 接口一次返回：

- 1min bars；
- session VWAP；
- 全部有效 Tick 的轻量数组；
- Profile histogram；
- VPOC、VAL、VAH zones；
- 下午首次触碰方向；
- 数据质量统计。

MVP 不提供独立的详细 Tick API、LOD 参数或缩放后重新请求。单个目标 CSV 的 Tick 数量在当前数据规模下可以直接由浏览器处理。

服务不提供任意文件读取接口，所有数据访问都必须通过合法日期和合约索引。

## 8. 前端设计

页面采用工作台布局：

```text
顶部：交易日、合约、数据质量

主图：
  1min K 线
  Tick path
  session VWAP
  上午/下午分界
  zone 区间
  support/resistance 首次触碰点

右侧：
  Volume Profile
  VPOC / VAL / VAH
  zone 列表

底部：
  1min volume
```

交互要求：

- 日期和合约选择后自动加载 chart payload；
- 图表与 zone 列表双向高亮；
- Profile 和 VWAP 支持显示/隐藏；
- 支撑、阻力、neutral 使用不同颜色；
- tooltip 显示时间、OHLC、成交量和 VWAP；
- 页面明确显示“估算 VWAP”而不是伪装成逐笔真实 VWAP。

## 9. 缓存策略

MVP 只使用进程内内存缓存，不写磁盘缓存：

```text
Map<date + instrument, { sourceMtime, sourceSize, result }>
```

缓存键包含：

```text
source path
source size
source mtime
algorithm version
```

计算流程：

1. 请求到达；
2. 检查内存缓存；
3. 命中且 source fingerprint 未变化则直接返回；
4. 未命中则读取一个 CSV 并计算；
5. 将结果放入内存缓存并返回。

MVP 不做磁盘缓存、压缩文件、单飞队列和跨重启缓存。后续在实测响应时间不足时再增加这些机制。

## 10. 局域网运行

默认开发地址：

```text
http://127.0.0.1:4317
```

局域网模式：

```bash
KLINE_HOST=0.0.0.0 \
KLINE_PORT=4317 \
KLINE_TICK_ROOT=/srv/kline/canonical/test/tick \
npm start
```

访问地址：

```text
http://<服务器局域网IP>:4317
```

安全边界：

- 只允许读取配置的数据根目录；
- 所有日期和合约参数严格校验；
- 不提供任意文件下载；
- 第一版不提供远程执行 pipeline；
- 第一版不提供远程写文件和交易操作；
- 防火墙只开放局域网所需端口。

## 11. 测试规划

### 单元测试

- 日期解析和交易时段边界；
- 价格 tick 对齐和价格分桶；
- cumulative volume 差分；
- volume reset；
- 1min OHLCV；
- 空分钟不补齐；
- session VWAP；
- VPOC、VAL、VAH；
- support/resistance first touch；
- 路径穿越和非法参数。

### Fixture 测试

准备一个小型四列 CSV，覆盖：

- 开盘前 Tick；
- 上午成交；
- 午休；
- 下午触碰 zone；
- 收盘后 Tick；
- 成交量 reset；
- 时间间隔较大的 Tick。

### 集成验收

至少验证：

1. 一个股指合约；
2. 一个国债合约；
3. 一个商品合约；
4. 一个有数据缺口的日期；
5. 多个客户端同时访问同一分析结果；
6. 修改原始文件后内存缓存自动失效。

## 12. 实施阶段

### 阶段一：项目骨架

- 创建 Node/TypeScript/Fastify 项目；
- 加载配置；
- 实现目录扫描；
- 实现 `/api/health`、`/api/days`；
- 加入路径安全测试。

### 阶段二：计算引擎和 API

- 实现流式 CSV reader；
- 实现标准化 Tick；
- 实现 1min K 线；
- 实现 session VWAP；
- 实现 Volume Profile 和 Value Area；
- 实现 VPOC、VAL、VAH 和首次触碰方向；
- 实现 `/api/chart/:date/:instrument`。

### 阶段三：前端

- 引入本地 ECharts；
- 实现日期/合约选择；
- 实现 K 线、Tick、VWAP、Profile 和 zone；
- 实现数据质量提示。

### 阶段四：局域网部署和验收

- 支持 `KLINE_HOST=0.0.0.0`；
- Node 24 生产构建；
- 用全部目标数据做目录和单文件响应测试；
- 验证手机/桌面浏览器局域网访问；
- 记录启动命令和 systemd 部署方式。

## 13. 明确不做的事情

- 不把近似 VWAP 称为真实逐笔 VWAP；
- 不从 Volume 猜测 Open Interest；
- 不伪造 Turnover；
- 不自动选择“主力合约”替代用户选择的具体合约；
- 不实现 HVN/LVN；
- 不计算 bounce、break、timeout、MFE、MAE；
- 不做磁盘缓存、SQLite、Review、SSE 和任务编排；
- 不做 Tick 降采样和独立局部 Tick API；
- 不做交易信号、收益统计和自动下单；
- 不修改原始 CSV。

## 14. 当前设计决策总结

```text
数据源       /srv/kline/canonical/test/tick
输入字段     update_date, symbol, last_price, volume
成交量语义   cumulative volume
K 线周期     1 minute
Profile      上午 Volume Profile
VWAP         Volume-weighted snapshot estimate
Profile 方法 VPOC / VAL / VAH
S/R 语义     上午 zone + 下午首次触碰方向
后端         Node 24 + TypeScript + Fastify
前端         原生 HTML/CSS/JS + 本地 ECharts
缓存         进程内按日期/合约缓存
部署         局域网只读 HTTP 服务
```

后续可按实际使用反馈增加 HVN/LVN、事件结果、磁盘缓存、Tick 降采样和人工 Review；这些能力不属于 MVP 的验收范围。
