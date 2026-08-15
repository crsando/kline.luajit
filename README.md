# kline.luajit

> 面向**国内期货**的 LuaJIT K 线内存库：在进程内维护多品种、多周期 K 线，支持 tick 实时合成、
> 交易时段感知、多周期 roll-up 派生，以及可读的 CSV 落地与回放。追求低 GC、高吞吐。

**状态**：M0–M5 全部完成 ✅　10 个 spec、**192 断言全绿**（LuaJIT 2.1）。
tick → 1m 合成 → 5m/15m 派生 → CSV 落地 → 加载回放，端到端跑通。

---

## ✨ 特性

- **FFI 定长 struct**：一根 Bar 88 字节、字段自然对齐，连续数组存储，低 GC、cache 友好。
- **Tick → 1m 合成**：时段过滤脏 tick、volume 差值累加（CTP 累计值）、跨分钟 + 主动 flush 封口。
- **多周期 roll-up**：1m → 5m/15m/30m/60m，时钟对齐 + **休盘/午休/收盘边界强制封口**、缺失不补空。
- **交易时段与日历**：`in_session` 判定、夜盘 `trading_day` 正确归属下一交易日（只处理周末）。
- **可读落地**：CSV（带 `#` 元信息头、定点价格），pandas / DuckDB / Excel 直读；按 `trading_day` 切分、以 `bar_time` 为主键幂等更新。
- **零第三方依赖**：纯 LuaJIT + 内置 `bit` 库；测试用极简自研断言（不依赖 busted）。

## 🧱 目录结构

```
kline/                 库源码（13 个模块）
├── types.lua          FFI cdef：kline_bar_t(88B) + 周期/位标志枚举
├── util.lua           时间转换 / minute_of_day / 位操作 / 日志
├── bar.lua            单根 bar 构造 / 读写 / 转换
├── series.lua         单条时间序列：FFI 数组 / 倍增扩容 / 二分 / 切片
├── store.lua          多 (symbol,period) 序列容器
├── calendar.lua       交易时段判定 + trading_day 归属
├── config.lua         品种交易时段表
├── aggregator.lua     tick → 1m 合成状态机
├── period.lua         1m → N分钟 roll-up
├── pipeline.lua       编织层：tick → 1m → 多周期 → store
├── codec.lua          CSV 编解码
├── persist.lua        按 trading_day 切分、bar_time upsert / 加载
└── init.lua           聚合 API（require "kline"）
spec/                  纯 Lua 单元测试 + t.lua 断言 helper
tools/akshare_data/    uv 子项目：抓取新浪期货分钟线作为历史测试数据
docs/                  设计稿 / 平台调研 / 开发计划
run_tests.sh           一键跑全部 spec
```

## 🚀 快速开始

```lua
local kline = require("kline")

local store = kline.new()
-- 为合约建一条 tick→多周期 流水线(自动按品种加载交易时段)
local pipe = kline.pipeline(store, "rb2510", { periods = { 5, 15 } })

-- 喂 tick(time 为 Unix 毫秒;volume 为当日累计值,内部自动取差值)
pipe:on_tick{ time = 1753318801000, price = 3200, volume = 10, open_interest = 5000 }
-- ... 更多 tick ...
pipe:flush()   -- 收盘 / 换节时主动封口

-- 取各周期序列
local s1  = store:get("rb2510", "1m")
local s15 = store:get("rb2510", "15m")
print(s1:count(), s1:last().close)

-- 落地为 CSV(按 trading_day 切分)
kline.persist.persist(s1, "data", { price_fmt = "%.1f" })
```

Python 侧读落地文件：

```python
import pandas as pd
df = pd.read_csv("data/rb2510_1m_20260724.csv", comment="#", parse_dates=["bar_time"])
```

## 🧪 测试

```bash
bash run_tests.sh          # 在 kline 项目根目录执行(需 luajit 在 PATH)
```

跑全部 10 个 spec，共 192 断言。

## 📐 行情字段（kline_bar_t，88 字节）

| 字段 | 类型 | 说明 |
|---|---|---|
| bar_time | int64 | bar 起始时间，Unix 毫秒（左闭） |
| open/high/low/close | double | OHLC |
| volume / turnover | double | 成交量（差值累加）/ 成交额 |
| open_interest | double | 持仓量（时点值取最新） |
| settlement | double | 结算价（日线） |
| trading_day | int32 | 交易日 YYYYMMDD（夜盘归属单独存） |
| tick_count | int32 | 本 bar tick 数 |
| flags | uint8 | 位标志：已封口 / 集合竞价 / 夜盘 |

> symbol 不进 struct，存在 Series header 里共用。

## ⚠️ 设计要点（避坑，源自 vnpy 等平台实战）

1. **区间语义全库统一左闭右开 `[start, end)`**（避免 vnpy 小时线多算一根）。
2. **bar 时间戳用起始时间**（左对齐）。
3. **volume 用差值累加**（CTP 推的是当日累计值，直接加会爆表）。
4. **封口分层**：tick→1m 靠「跨分钟 + flush」；1m→Nm 才用休盘边界 `is_last_min_of_segment`
   （每分钟一根，不会重复触发）。
5. **休盘不补空 bar**；缺失 1m 直接跳过。
6. **trading_day 优先用 CTP 字段**，夜盘归属下一交易日；只处理周末，不处理长假。

## 📚 文档

- **本地共享数据目录、文件格式和并发约定见 [DATABASE.md](DATABASE.md)。**
- `docs/design.md` — 库设计稿（架构 / 字段 / API / 落地格式）
- `docs/platforms_research.md` — vnpy 等国内平台 K 线聚合调研与踩坑
- `docs/tradingview_research.md` — TradingView 对齐哲学对比
- `docs/DEVELOPMENT.md` — 分里程碑开发计划与验收
- `tools/akshare_data/README.md` — AKShare 新浪期货测试数据工具与格式差异

## 🗺️ Roadmap

- [ ] 日线聚合（按 trading_day，含夜盘，开盘价=夜盘首根）
- [ ] off-heap 内存（ffi.C.malloc）绕开 LuaJIT 2GB 堆限制
- [ ] 二进制 IPC：定长内存块直传 Node.js（零序列化）
- [ ] Parquet 归档脚本（DuckDB）
- [ ] 非整除周期（90分钟）交易时长对齐
- [ ] 真实 CTP 行情回放

## License

MIT（自用/内部）
