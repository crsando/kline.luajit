# K 线数据契约

本文定义当前 `kline.luajit` 项目中一根 K 线（Bar）的数据结构、字段语义、时间约定、
聚合规则、序列约束和持久化边界。本文以当前代码为准；共享目录、锁和文件生命周期另见
[`DATABASE.md`](DATABASE.md)。

## 1. 核心定义

### 1.1 Bar 的区间语义

完整且正常对齐的 Bar 统一使用左闭右开区间：

```text
[bar_time, bar_time + period)
```

`bar_time` 永远表示 Bar 的开始时间，不表示结束时间。例如：

- `09:00` 的 1 分钟 Bar 覆盖 `[09:00:00, 09:01:00)`。
- `09:00` 的 5 分钟 Bar 覆盖 `[09:00:00, 09:05:00)`。
- 休盘前被提前封口的高周期 Bar 可能短于名义周期。

当前 Rollup 不补缺失的 1 分钟 Bar。如果名义窗口开头缺失，高周期 `bar_time` 会采用
实际收到的第一根 1 分钟 Bar，而不是伪造名义窗口起点；这种 Bar 是部分窗口，不能再
单纯用 `bar_time + period` 推导其实际覆盖范围。

外部数据如果使用结束时间标签，必须先转换成开始时间。新浪 1 分钟线中的 `09:01`
对应本项目的 `09:00`，导入 canonical 数据前应减 1 分钟。

### 1.2 Bar 的身份

一根 Bar 的逻辑主键是：

```text
(symbol, period, bar_time)
```

`trading_day` 是分区和业务归属字段，不是主键的一部分。同一主键的新内容表示对已有
Bar 的修正，持久化时覆盖旧值，而不是追加一根重复 Bar。

### 1.3 三层数量语义

同名字段在 tick、Bar 和外部数据中可能有不同语义：

| 层级 | `volume` / `turnover` 语义 |
|---|---|
| CTP tick 输入 | 当前交易日累计值 |
| 内部 K 线 Bar | 本 Bar 区间内的增量 |
| 外部分钟线 | 由数据源定义；新浪分钟线已经是每根 Bar 的增量 |

因此，tick 合成时需要计算累计值之差；导入已经聚合好的分钟线时不能再次做累计差分。

## 2. 内存结构

一根 Bar 使用 LuaJIT FFI 的定长结构 `kline_bar_t`：

```c
typedef struct {
    int64_t  bar_time;
    double   open;
    double   high;
    double   low;
    double   close;
    double   volume;
    double   turnover;
    double   open_interest;
    double   settlement;
    int32_t  trading_day;
    int32_t  tick_count;
    uint8_t  flags;
    uint8_t  _pad[7];
} kline_bar_t;
```

结构大小固定为 88 字节，按 8 字节对齐。字段偏移如下：

| 偏移 | 字段 | C 类型 | 定义 |
|---:|---|---|---|
| 0 | `bar_time` | `int64_t` | Bar 开始时间，Unix 毫秒 |
| 8 | `open` | `double` | 区间内第一笔有效价格 |
| 16 | `high` | `double` | 区间内最高有效价格 |
| 24 | `low` | `double` | 区间内最低有效价格 |
| 32 | `close` | `double` | 区间内最后一笔有效价格 |
| 40 | `volume` | `double` | 本 Bar 成交量增量 |
| 48 | `turnover` | `double` | 本 Bar 成交额增量 |
| 56 | `open_interest` | `double` | Bar 结束时最新持仓量 |
| 64 | `settlement` | `double` | 结算价；分钟线通常为 `0` |
| 72 | `trading_day` | `int32_t` | 交易日，格式 `YYYYMMDD` |
| 76 | `tick_count` | `int32_t` | 聚合进本 Bar 的 tick 数 |
| 80 | `flags` | `uint8_t` | Bar 状态位 |
| 81 | `_pad` | `uint8_t[7]` | 内存对齐填充，无业务含义 |

`symbol` 和 `period` 不存入每根 Bar，而是存放在所属 `Series` 上，避免每根记录重复保存。

### 2.1 字段约束

有效 Bar 应满足以下语义约束：

```text
bar_time > 0
trading_day 为有效的 YYYYMMDD
open/high/low/close 为有限数
open/high/low/close > 0
high >= max(open, close, low)
low <= min(open, close, high)
volume >= 0
turnover >= 0
open_interest >= 0
settlement == 0 或 settlement > 0
tick_count >= 0
0 <= flags <= 255
```

其中要注意：

- `bar.from_table()` 只负责类型转换，缺失字段填 `0`，不校验上述关系。
- `Series:append()` 只做二进制复制，也不校验字段。
- tick 聚合器会保证由有效 tick 生成的 OHLC 关系，并将负成交差值截为 `0`。
- CSV 解码主要验证列数、时间可解析和数值可转换，不做完整行情合理性检查。
- 外部数据导入方必须自行校验 NaN、无穷值、负成交量和 OHLC 关系。

`CLOSED` 表示 Bar 已完成聚合、可以消费和持久化，但不表示历史数据永远不可修正；同一
`bar_time` 后续仍可用新内容覆盖。

## 3. 时间与交易日

### 3.1 内部时间

- 内部 `bar_time` 使用 Unix 毫秒，存入 `int64_t`。
- 默认交易所时区固定为北京时间 `UTC+8`，即 `28800` 秒。
- CSV 中将 `bar_time` 格式化为北京时间 `YYYY-MM-DD HH:MM:SS`。
- 当前 CSV 格式只有秒精度；内部带毫秒余数的时间在落盘后会丢失余数。
- 正常分钟 Bar 应对齐到整分钟，因此不应依赖毫秒余数。

`bar_time` 的自然日期不能替代 `trading_day`。例如周五晚上的夜盘 Bar，其自然日期仍是
周五，但通常属于下周一交易日。

### 3.2 交易时段

交易时段字符串格式为：

```text
HH:MM-HH:MM,HH:MM-HH:MM,...
```

每段同样是左闭右开。`09:00-10:15` 包含 `09:00` 到 `10:14`，不包含 `10:15`。
当开始分钟大于结束分钟时表示跨午夜，例如 `21:00-02:30`。

当前内置配置只有：

| 品种键 | 时段 |
|---|---|
| `rb`、`hc` | `21:00-23:00,09:00-10:15,10:30-11:30,13:30-15:00` |
| `IF`、`IC`、`IH` | `09:30-11:30,13:00-15:00` |
| `DAY` | `09:00-10:15,10:30-11:30,13:30-15:00` |

未知品种回退到 `DAY`。当前配置查找存在大小写差异：`rb2510` 能匹配 `rb`，但
`RB2510` 不会自动匹配小写 `rb`，会回退到 `DAY`。调用方应传入与配置键一致的 symbol，
或显式传入 `sessions` / `Calendar`。

### 3.3 `trading_day` 规则

优先级如下：

1. tick 中显式提供的 `trading_day`，通常来自 CTP。
2. 没有外部交易日时，由 `Calendar` 根据北京时间推算。

当前内置推算规则是：

- 日盘归属自然日。
- `night_start` 之后的夜盘归属下一个工作日。
- 只跳过周六和周日，不处理中国法定节假日和临时休市。

对于跨午夜品种，内置 fallback 不能完整处理周五午夜后的 Bar：午夜后的时间不满足
“大于 night_start”，可能被归到自然日。此类品种必须优先使用 CTP `trading_day` 或外部
交易日历。

## 4. 周期表示

数值周期统一表示分钟数：

| 常量 | 分钟数 | `PERIOD_NAME` |
|---|---:|---|
| `M1` | 1 | `1m` |
| `M5` | 5 | `5m` |
| `M15` | 15 | `15m` |
| `M30` | 30 | `30m` |
| `H1` | 60 | `1h` |
| `H2` | 120 | `2h` |
| `H4` | 240 | `4h` |
| `D1` | 1440 | `1d` |

需要区分两种表示：

- `Rollup.new(period_min, ...)` 使用数值分钟数。
- `Series.period` 和文件名使用字符串标签。

当前 `Pipeline` 直接用 `tostring(period_min) .. "m"` 创建派生序列，因此 60 分钟会得到
`60m`；`PERIOD_NAME[60]` 则是 `1h`。代码不会自动把两者归一化，调用方必须选定并坚持
一种标签，不能把 `60m` 和 `1h` 当成同一个 Series。

`D1=1440` 当前只是保留的日线哨兵值。项目尚未实现按 `trading_day` 的正式日线聚合，
不能直接把普通分钟 Rollup 当成正确的期货日线。

## 5. Flags

`flags` 是位掩码，可以组合：

| 名称 | 值 | 含义 |
|---|---:|---|
| `CLOSED` | `0x01` | Bar 已封口 |
| `AUCTION` | `0x02` | 集合竞价 Bar |
| `NIGHT` | `0x04` | 夜盘 Bar |

示例：`5` 等于 `CLOSED | NIGHT`。

当前行为边界：

- tick 聚合器在封口时自动设置 `CLOSED`。
- tick 时间大于等于 `night_start` 时自动设置 `NIGHT`。
- 午夜后的跨夜时段不会被当前自动逻辑标记为 `NIGHT`，需要外部修正。
- `AUCTION` 当前没有自动判定逻辑，只能由数据源或调用方设置。
- 高周期 Rollup 当前不会传播 1 分钟 Bar 的 `NIGHT` 或 `AUCTION`，只在自身封口时设置
  `CLOSED`。

## 6. Tick 到 1 分钟 Bar

### 6.1 Tick 输入契约

```lua
{
  time = 1753318801000,       -- 必填，Unix 毫秒
  price = 3200,               -- 必填，有效值必须 > 0
  volume = 100,               -- 应提供，交易日累计成交量
  turnover = 320000,          -- 应提供，交易日累计成交额
  open_interest = 5000,       -- 可选，最新持仓量
  trading_day = 20260724,     -- 可选，优先于 Calendar 推算
}
```

虽然代码允许缺少 `volume` 和 `turnover` 并按 `0` 处理，但实时流中不应间歇性省略：一次
缺失会改变下一笔 tick 的差分基准，造成错误增量。

### 6.2 无效 Tick 过滤

聚合器按以下顺序过滤：

1. 缺少价格或 `price <= 0`。
2. 时间小于上一笔有效 tick，即时间倒退。
3. 时间不在配置的交易时段中。

等时间戳 tick 不会被过滤。被过滤的 tick 不更新成交量、成交额和时间基准。

### 6.3 聚合规则

第一笔有效 tick 打开当前分钟：

```text
open = high = low = close = tick.price
open_interest = tick.open_interest 或 0
tick_count = 1
```

同一分钟后续 tick：

```text
high = max(high, tick.price)
low = min(low, tick.price)
close = tick.price
volume += max(tick.volume - last_tick.volume, 0)
turnover += max(tick.turnover - last_tick.turnover, 0)
open_interest = 最新非 nil 值
tick_count += 1
```

聚合器启动后的第一笔有效 tick 只建立累计值基准，其 `volume` 和 `turnover` 增量为 `0`。
因此进程在交易时段中途启动时，不会把启动前的累计成交错误计入第一根 Bar，但第一根
Bar 也不会包含建立基准之前的成交。

累计值跨交易日归零时差值为负，当前实现将该差值截为 `0`。

### 6.4 封口规则

1 分钟 Bar 在以下情况封口：

- 下一笔有效 tick 进入更晚的分钟。
- 调用方主动调用 `flush()`。

非交易时段 tick 会被过滤，不会自动触发封口。若收盘后没有下一笔有效 tick，调用方
必须在休盘、收盘或数据流结束时调用 `flush()`，否则最后一根 Bar 会留在聚合器内部。

当前分钟没有 tick 就不生成 Bar；跨越多个空分钟时不会补零量空 Bar。

## 7. 1 分钟到高周期 Rollup

Rollup 的输入契约是已经封口、按 `bar_time` 升序到达的 1 分钟 Bar。代码当前不会检查
输入是否带 `CLOSED`，该要求由调用方保证。

### 7.1 对齐规则

窗口使用北京时间墙上时钟对齐：

```text
window_key = floor(minute_of_day / period_min)
```

例如 15 分钟窗口为 `09:00-09:14`、`09:15-09:29`。实现假定交易时段起点适合目标周期
的时钟对齐；它不是“从每个交易段起点重新累计 N 分钟”的通用算法。

### 7.2 字段聚合

| 字段 | 高周期规则 |
|---|---|
| `bar_time` | 实际收到的第一根 1 分钟 Bar 的时间 |
| `trading_day` | 第一根 1 分钟 Bar 的交易日 |
| `open` | 第一根的 `open` |
| `high` | 所有输入的最大 `high` |
| `low` | 所有输入的最小 `low` |
| `close` | 最后一根的 `close` |
| `volume` | 求和 |
| `turnover` | 求和 |
| `open_interest` | 最后一根的值 |
| `settlement` | `0` |
| `tick_count` | 求和 |
| `flags` | 封口后只有 `CLOSED` |

如果窗口起始的 1 分钟 Bar 缺失，例如 5 分钟窗口第一根收到的是 `09:03`，高周期
`bar_time` 会是 `09:03`，而不是名义窗口起点 `09:00`。项目不伪造缺失数据。

### 7.3 高周期封口

高周期在以下情况封口：

- 输入进入新的时钟窗口。
- 当前 1 分钟 Bar 是连续交易段的最后一分钟。
- 调用方调用 `Rollup:flush()`。

Rollup 的窗口键只包含天内分钟，不包含日期和 `trading_day`。如果数据末尾缺少交易段
最后一分钟，又没有调用 `flush()`，下一交易日相同窗口可能被错误合并。因此每次回放
结束、断流或交易段结束时必须确保封口。

## 8. Series 与 Store

每个 `Series` 表示一条 `(symbol, period)` 时间序列，底层是连续 FFI 数组：

- 默认初始容量为 256 根。
- 容量不足时按 2 倍扩容。
- `at(i)` 使用 1-based 索引；负数从尾部计数，`-1` 表示最后一根。
- `last()` 返回最后一根，空序列返回 `nil`。
- `find(bar_time)` 使用二分查找精确时间。
- `slice(from_t, to_t)` 返回 `[from_t, to_t)` 对应的起始索引和数量，不复制数据。
- `each()` 按数组顺序遍历。

### 8.1 Series 调用方约束

`Series:append()` 不检查时间顺序和重复主键。为了保证 `find()` 和 `slice()` 正确，调用方
必须保证：

```text
bar_time 单调非递减；正常情况下应严格递增且唯一
```

从 CSV `load_into()` 时同样只是逐行 append，不会自动排序、去重，也不验证文件元信息
是否与目标 Series 的 symbol/period 一致。

`at()`、`last()` 和 `append()` 返回的是底层元素指针。后续 append 可能触发扩容并替换
底层数组，因此不要跨可能扩容的操作长期保存旧指针。需要长期保存时应复制 Bar 值或
重新按索引获取。

`Store` 以字符串 `symbol .. "|" .. period` 为键。symbol 或 period 中如果包含 `|` 会造成
键歧义，当前代码不做防护；正常合约和周期标签不得包含该字符。

## 9. Pipeline 行为

`Pipeline` 将以下步骤串联：

```text
tick -> Aggregator -> 已封口 1m -> Rollup -> 已封口 Nm -> Store
```

- 当前未封口的 1 分钟 Bar 只存在于 `Aggregator.cur`，不会进入 Series。
- 1 分钟 Bar 封口后先 append 到 `symbol/1m` Series，再送入所有 Rollup。
- 默认派生周期为 5 分钟和 15 分钟。
- `Pipeline:flush()` 先封口当前 1 分钟 Bar，再封口所有尚未完成的高周期 Bar。
- Series 中原则上只保存已封口 Bar；手工调用 `Series:append()` 时需由调用方保证。

## 10. CSV 契约

核心 CSV 固定为 12 列，顺序不可改变：

```text
bar_time,trading_day,open,high,low,close,volume,turnover,
open_interest,settlement,tick_count,flags
```

实际文件中列名位于同一行；上面仅为文档换行。

### 10.1 核心 `codec` 格式

`kline.codec` 生成的文件头为：

```csv
# symbol=rb2510 period=1m tz=Asia/Shanghai generated_by=kline.lua
bar_time,trading_day,open,high,low,close,volume,turnover,open_interest,settlement,tick_count,flags
```

默认格式化精度：

| 字段 | 格式 |
|---|---|
| OHLC、`settlement` | `price_fmt`，默认 `%.2f` |
| `volume`、`open_interest` | `%.0f` |
| `turnover` | `%.2f` |
| `tick_count`、`flags` | 十进制整数 |

该 CSV 是可读、定点格式，不是 double 的无损序列化。低于格式精度的变化会被舍入，
持久化的“内容是否变化”也是比较格式化后的 CSV 行。

解析器跳过空行、`#` 注释行和以 `bar_time` 开头的列名行。不存在的文件读取为零根；
损坏的数据行会报错，而不是静默填 `0`。

### 10.2 核心 `persist` 规则

`kline.persist.persist(series, dir)`：

1. 只收集带 `CLOSED` 的 Bar。
2. 按 `trading_day` 分文件。
3. 文件名为 `{symbol}_{period}_{trading_day}.csv`。
4. 以内存和磁盘上的 `bar_time` 为唯一键。
5. 同一 Series 内重复时间以后出现的 Bar 为准。
6. 磁盘文件内重复时间以后出现的行为准，并在重写时去重、排序。
7. 当前 Series 没有包含的历史磁盘记录继续保留。
8. 新内容覆盖同一时间旧内容，最终按 `bar_time` 升序。
9. 完全相同的数据不重写文件。

`persist()` 返回新增或内容变化的 Bar 数，不包含仅因旧文件重复或乱序而触发的整理数量。

### 10.3 核心持久化与共享归档的边界

当前有两个相关但不同的协议：

| 能力 | `kline.persist` | `DATABASE.md` canonical v1 / 新浪工具 |
|---|---|---|
| 12 列 Bar schema | 支持 | 支持 |
| `bar_time` upsert | 支持 | 支持 |
| 同目录临时文件 + rename | 支持 | 支持 |
| `flock` 多写入者保护 | 不支持 | 要求支持 |
| 文件 `fsync` | 不支持 | 要求支持 |
| `schema/source/source_time/calendar` 元信息 | 不写 | 必须写 |
| 来源、品种、周期、年份目录分区 | 不管理 | 统一管理 |

因此 `kline.persist` 适合单进程、本地工作目录；不能直接视为共享数据库的并发写入实现。
多个进程或多台机器共享写入时必须遵守 `DATABASE.md` 的完整锁和原子更新协议。

读取单个由原子替换发布的 canonical 文件不需要加锁；读取多个文件并要求同一批次一致性
时，需要版本化快照或 manifest。

## 11. 外部 K 线导入

外部数据进入内部或 canonical schema 前至少需要确认：

1. symbol 是具体合约还是连续合约，不能混写。
2. 周期标签与当前 Series 一致。
3. 数据源时间是开始时间还是结束时间。
4. 时间统一转换为北京时间语义下的 Bar 开始时间。
5. `volume` 是每根增量还是累计值。
6. 持仓字段正确映射为 `open_interest`。
7. 夜盘 `trading_day` 使用可靠交易日历或交易所字段。
8. 数据源缺少的字段用 `0` 表示“未提供”，不能伪造。
9. 已结束的历史 Bar 设置 `CLOSED`；夜盘按需要设置 `NIGHT`。
10. 导入结果按 `bar_time` 排序且主键唯一。

对于当前新浪 1 分钟工具：

```text
datetime       -> bar_time，减 1 分钟
open/high/low/close -> 同名字段
volume         -> volume，已经是每根增量
hold           -> open_interest
缺失字段        -> 0
flags          -> CLOSED，夜盘再加 NIGHT
```

## 12. 当前限制清单

调用方不能假设当前版本已经提供以下能力：

- Series 自动排序、自动去重或字段合法性校验。
- 中国法定节假日交易日历。
- 所有国内期货品种的准确交易时段。
- 跨午夜 fallback 的完整 `trading_day` 和 `NIGHT` 推断。
- 高周期对 `NIGHT`、`AUCTION` 标志的传播。
- 缺失分钟补齐或合成空 Bar。
- 正式的期货日线聚合。
- double 精度无损 CSV。
- 核心 `kline.persist` 的多进程写入锁和崩溃后持久性保证。

这些边界发生变化时，应同步更新本文件、测试和 schema 版本。
