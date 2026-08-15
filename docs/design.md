# kline.lua —— LuaJIT 内存 K 线库设计稿

> 目标：在 LuaJIT 进程内维护多品种、多周期的 K 线内存表，支持 tick 实时合成、
> 交易时段感知、增量落地与快速回放。面向交易系统，追求低 GC 压力和高吞吐。

---

## 0. 设计原则（先定调）

1. **内存要快、落地要"人和 Python 都能读"**：内存层仍用 FFI 定长 struct 保证低 GC、高吞吐；
   但落地**不用私有二进制**，改用带 header 的 CSV（温层）+ Parquet（冷层归档），
   保证 pandas/polars/DuckDB/Excel 都能直接打开分析。落地时多一步文本/列式序列化，这点开销换来的是可读性和互操作，值。
2. **每个 (symbol, period) 一条独立时间序列**：内部叫一个 `Series`，各自持有自己的连续数组。
3. **Append-only 为主**：K 线天然是追加写。用**预分配 + 倍增扩容**的 FFI 数组，避免 Lua table 的哈希开销。
4. **时段与日历外置**：合成器只管 OHLC 状态机，"能不能交易 / 归属哪个交易日"交给 calendar 模块，换品种只换配置。
5. **分层可选**：内存层可以单独用；落地层、tick 合成层、时段层都是可插拔的。

---

## 1. 文件拆分建议

```
kline/
├── init.lua          -- 库入口，对外聚合 API（require "kline" 就拿到全部）
├── types.lua         -- FFI cdef：Bar struct、Series header、枚举常量
├── bar.lua           -- 单根 Bar 的构造/读写/拷贝辅助函数
├── series.lua        -- 核心：单条 (symbol,period) 时间序列的内存管理（扩容/追加/查询/二分定位）
├── store.lua         -- 多 Series 容器：按 symbol+period 建/取/遍历 Series，全局管理
├── aggregator.lua    -- tick -> bar 合成状态机（依赖 calendar 判定时段边界）
├── calendar.lua      -- 交易日历 + 交易时段：in_session / is_session_end / trading_day 映射
├── period.lua        -- 周期定义与时间对齐（1m/5m/15m/1h/1d，把 timestamp 向下取整到 bar_start）
├── codec.lua         -- 落地编解码：CSV(默认)/JSONL/Parquet 后端的读写、文件头元信息、格式化
├── persist.lua       -- 落地调度：flush 策略（定时/满 N 根/收盘）、文件切分（按天/按品种）、加载回放
├── config.lua        -- 品种时段表、周期表、落地路径、flush 策略等配置
└── util.lua          -- 时间工具、日志、错误处理小函数
```

如果想更轻量，最小可用集合只要 4 个：`types.lua` + `series.lua` + `store.lua` + `init.lua`（纯内存表）。
tick 合成、时段、落地按需再加。

---

## 2. 行情字段设计（Bar struct）

用 FFI struct，定长、紧凑、可直接落地。针对国内期货 + 通用性设计：

```c
// types.lua 里的 ffi.cdef
#pragma pack(1)
typedef struct {
    int64_t  bar_time;      // bar 起始时间，Unix 毫秒（对齐后的时段轴时间）
    int32_t  trading_day;   // 交易日 YYYYMMDD（夜盘归属很关键，单独存）
    double   open;
    double   high;
    double   low;
    double   close;
    double   volume;        // 成交量
    double   turnover;      // 成交额（金额）
    double   open_interest; // 持仓量（期货必备）
    double   settlement;    // 结算价（日线才有意义，分钟线可置 0）
    int32_t  tick_count;    // 本 bar 累计 tick 数，用于判断有效性/调试
    uint8_t  flags;         // 位标志：bit0=已封口 bit1=集合竞价 bit2=夜盘 ...
    uint8_t  _pad[3];       // 对齐填充，凑成整齐字节数
} kline_bar_t;              // 固定 88 字节/根
```

字段说明：
- **bar_time**：统一用毫秒时间戳，避免时区/字符串解析。展示时再格式化。
- **trading_day**：单独一列，因为夜盘时间的自然日 ≠ 交易日（周五夜盘算下周一），持久化和查询都要靠它。
- **OHLC + volume + turnover + open_interest**：期货核心五件套，`turnover` 和 `open_interest` 股票可忽略。
- **settlement**：日线结算价，分钟线留 0。
- **tick_count / flags**：运维和数据质量用，`flags` 用位标志省空间。
- **symbol 不进 struct**：symbol 存在 Series 的 header 里（整条序列共用一个），不必每根 bar 重复存字符串。

> 想再省空间可以把 double 换成 int64 定点价（价格 * 10^n），期货 tick size 固定，定点更精确也更小。
> 先按 double 给你，通用、简单。

---

## 3. 主要 API 入口

对外统一从 `require "kline"` 拿。分四组：

### 3.1 Store / Series 管理（内存表核心）
```lua
local kline = require "kline"

-- 打开/创建一个全局 store（可传配置）
local store = kline.new(config)

-- 取一条时间序列（不存在则创建），period 如 "1m"/"5m"/"1d"
local s = store:series("rb2510", "1m")

-- 直接塞一根已成型的 bar（回放/导入历史用）
s:append(bar)            -- bar 为 table 或 FFI struct

-- 查询
s:last()                 -- 最新一根 bar
s:at(i)                  -- 第 i 根（支持负索引，-1 = 最新）
s:count()                -- 根数
s:slice(from_time, to_time)  -- 时间区间切片，返回视图/迭代器
s:find(bar_time)         -- 二分定位某个时间的 bar 索引
```

### 3.2 Tick 合成（实时）
```lua
-- 喂 tick，内部按品种时段合成；返回"刚封口的 bar"（没有则 nil）
local closed = store:on_tick{
    symbol = "rb2510",
    time   = ts_ms,        -- 交易所时间戳（毫秒）
    price  = 3210.0,
    volume = 12,
    turnover = 38520.0,
    open_interest = 210345,
}
-- closed ~= nil 时，说明上一根已封口，可在回调里落地/推送

-- 也支持注册回调，封口即回调（推给 Node.js / 落地 / 计算指标）
store:on_bar_closed(function(series, bar) ... end)

-- 收盘或换节时，强制封口当前未完成 bar
store:flush_open_bars()
```

### 3.3 落地 / 加载
```lua
-- 手动落地：已封口 bar 按 trading_day 分文件，以 bar_time 为主键 upsert
kline.persist.persist(s, "data", { price_fmt = "%.1f" })

-- 从落地文件加载回内存（启动预热 / 回测）
kline.persist.load(s, "data", 20260723)

-- 直接读取一个文件为 bar table 数组
local bars = kline.persist.read_file("data/rb2510_1m_20260723.csv")
```

### 3.4 交易时段 / 日历（可单独用）
```lua
local cal = kline.calendar.load("rb")      -- 加载 rb 螺纹钢的时段表
cal:in_session(ts_ms)                       -- 该时刻是否可交易（过滤脏 tick）
cal:is_session_end(ts_ms)                   -- 是否命中收盘时刻（触发封口）
cal:trading_day(ts_ms)                      -- 返回该 tick 归属的交易日 YYYYMMDD
```

---

## 4. 落地文件格式（可读优先 + Python 友好）

放弃私有二进制，采用**两层文本/列式格式**，让 pandas / polars / DuckDB / Excel 都能直接读：

### 4.1 温层：CSV（默认落地格式，`.csv`）

实时增量落地用 CSV。每个交易日只有数百根分钟 bar，落地时读取当日文件、按 `bar_time` 合并并排序，最后通过同目录临时文件原子替换。这样进程重启后重复调用保持幂等，也支持修正历史 bar。

```
# 文件头两行：注释元信息（# 开头，pandas 用 comment='#' 跳过）+ 列名行
# symbol=rb2510 period=1m tz=Asia/Shanghai generated_by=kline.lua v1
bar_time,trading_day,open,high,low,close,volume,turnover,open_interest,settlement,tick_count,flags
2026-07-23 09:00:00,20260723,3210.0,3215.0,3208.0,3212.0,1520,48792000,210345,0,142,1
2026-07-23 09:01:00,20260723,3212.0,3218.0,3211.0,3216.0,980,31488000,210410,0,88,1
...
```

字段约定（关键，保证 Python 侧无歧义）：
- **bar_time**：写成 `YYYY-MM-DD HH:MM:SS` 可读字符串（不是毫秒时间戳），pandas `parse_dates` 直接认。
  想要极致精度可再加一列 `bar_time_ms` 存毫秒整数，两全其美。
- **trading_day**：`YYYYMMDD` 整数，夜盘归属靠它，别靠 bar_time 的自然日。
- 价格用**定点小数字符串**格式化（按品种 tick size 决定小数位，如 `%.1f`），避免 double 打印出 `3210.0000001` 这种脏数据。
- **flags** 用小整数（位标志），CSV 里就是个数字，Python 侧按位解析。

Python 读取（你以后分析就这么用）：
```python
import pandas as pd
df = pd.read_csv("rb2510_1m_20260723.csv", comment="#", parse_dates=["bar_time"])
```

命名：`{symbol}_{period}_{trading_day}.csv`，例如 `rb2510_1m_20260723.csv`，按交易日切分。

> CSV 缺点是体积大、读大文件慢、无强类型 schema。所以它只做**温层**（当天/近期、边写边看），
> 长期历史不留在 CSV，定期滚进 Parquet。

### 4.2 冷层：Parquet（长期归档 + 高性能分析）

历史数据定期从 CSV 归档成 Parquet：列式存储、压缩比高（通常比 CSV 小 5–10 倍）、带 schema、
pandas/polars/DuckDB 读取快几个数量级，是做历史行情量化分析的事实标准。

**由谁来写 Parquet？** LuaJIT 没有好用的原生 Parquet 库，所以**不在 Lua 侧硬写**，而是走"导出通道"：
- 方案 A（推荐，简单）：Lua 只管写 CSV；一个独立的 **Python/DuckDB 归档脚本**定期把 CSV 批量转 Parquet。
  DuckDB 一条 SQL 就能转：`COPY (SELECT * FROM 'xx.csv') TO 'xx.parquet' (FORMAT parquet, COMPRESSION zstd);`
- 方案 B（进阶）：LuaJIT 通过 FFI 绑定 Arrow C GLib / DuckDB C API 直接写 Parquet，省掉中转，但工程量大，先不上。

归档后按 `symbol/period/year` 分区存放，方便按品种、周期、年份做分区裁剪：
```
archive/
  rb2510/1m/2026/rb2510_1m_2026.parquet
  rb2510/1d/rb2510_1d.parquet
```

DuckDB 直接跨文件查（做回测/分析爽点）：
```sql
SELECT * FROM 'archive/rb2510/1m/2026/*.parquet' WHERE trading_day BETWEEN 20260101 AND 20260630;
```

### 4.3 可选：JSON Lines（对接 / 调试用）

如果要跟别的服务交换或调试，`codec` 再挂一个 `.jsonl` 后端：每行一个 bar 的 JSON 对象，
人可读、流式、任何语言都能解析。不做默认，仅按需。

### 4.4 落地后端对比（codec 可插拔，配置里选）

| 后端 | 定位 | 谁写 | 优点 | 缺点 |
|---|---|---|---|---|
| **CSV** | 默认温层 | LuaJIT 原生 | 可读、Excel/pandas 直开、追加简单 | 体积大、读慢、无 schema |
| **Parquet** | 冷层归档 | Python/DuckDB 导出脚本 | 压缩好、分析极快、跨语言、带 schema | Lua 侧不直接写 |
| JSONL | 交换/调试 | LuaJIT | 可读、通用 | 冗余大 |
| `.kbar` 二进制 | （可选）高频回测 | LuaJIT | 最快、mmap | 私有、不可读 —— 默认不用 |

> **推荐分层**：热数据→内存 FFI 数组；温数据→当天 **CSV**（边写边看、pandas 直读）；
> 冷数据→定期滚成 **Parquet** 归档（长期分析）。既满足你"可读 + Python 能用"的要求，又不牺牲内存层性能。
> 这和上一版 Python demo 里 SQLite→Parquet 的分层思路一致，只是温层从二进制换成了更透明的 CSV。

---

## 5. 内存管理关键细节（series.lua）

- 每条 Series 持有一个 `ffi.new("kline_bar_t[?]", cap)` 的连续数组 + `len` + `cap`。
- 追加时 `len == cap` 就倍增扩容（`ffi.new` 新数组 + `ffi.copy` 搬迁），摊还 O(1)。
- 可选**环形缓冲**模式：只保留最近 N 根（实时盯盘不需要全历史时省内存）。
- 查询走二分（bar_time 单调递增），`slice` 返回轻量视图（起止索引 + 引用），不拷贝数据。
- Series header 只保留内存序列自身状态；持久化状态由磁盘文件中的 `bar_time` 决定，不依赖进程内水位。

---

## 6. Tick 合成状态机（aggregator.lua）要点

沿用我们上一轮聊的时段感知逻辑：
1. tick 进来先 `calendar:in_session(t)` 过滤脏 tick / 集合竞价异常价。
2. `period:align(t)` 把时间戳对齐到 bar_start。
3. 未跨界 → 更新 high/low/close，累加 volume/turnover，刷新 open_interest（取最新），tick_count++。
4. 跨界 or `calendar:is_session_end(t)` → 封口旧 bar（置 flags 已封口，append 进 Series，触发 on_bar_closed），用当前 tick 初始化新 bar。
5. **休盘 gap 不补空 bar**：时间轴是时段拼接的逻辑轴，10:15–10:30 没 tick 自然没 bar。

---

## 7. 一个典型使用流程

```lua
local kline = require "kline"
local store = kline.new(require "kline.config")

-- 启动预热：把今天已有的 bar 从磁盘加载回内存
store:load("rb2510", "1m")

-- 封口即落地 + 推送
store:on_bar_closed(function(series, bar)
    series:persist()                 -- 增量写 .kbar
    push_to_node(series.symbol, bar) -- 推给 Node.js 前端
end)

-- 行情线程里喂 tick
while true do
    local tick = recv_tick()
    store:on_tick(tick)
end

-- 收盘钩子
store:flush_open_bars()
store:persist()
```

---

## 8. 后续可扩展（先留接口）

- 多周期自动派生：1m 封口时自动 roll up 成 5m/15m/1d（`period.lua` 里配置派生关系）。
- 指标缓存层：在 Series 上挂 MA/EMA 等增量指标，封口时更新。
- 与 Node.js 的 IPC：封口 bar 通过你偏好的 length-prefixed + lua-seri 推送。
- mmap 只读加载：回测时把 .kbar 直接 mmap，零拷贝遍历。
```
