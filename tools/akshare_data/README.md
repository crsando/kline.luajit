# AKShare 新浪期货测试数据

这个独立 `uv` 子项目通过 [AKShare](https://github.com/akfamily/akshare) 的
`futures_zh_minute_sina` 接口抓取新浪内盘期货分钟行情，为 `kline.luajit` 提供近期历史测试数据。

数据只用于开发、回归测试和人工对照，不应作为交易或结算依据。

## 当前进度

- [x] 建立独立 `uv` 项目，固定 `akshare==1.18.87`。
- [x] 获取新浪期货 1/5/15/30/60 分钟行情。
- [x] 同时保存原始数据和 `kline.luajit` 兼容数据。
- [x] 按 `trading_day` 拆分，以 `bar_time` 为主键合并历史文件。
- [x] 对转换逻辑增加离线单元测试。
- [x] 用新浪真实交易段边界确认接口使用结束时间标签。
- [x] 接入 `KLINE_DATA_HOME` 共享目录、年份分区、原子替换和 `flock` 写锁。
- [ ] 增加数据质量报告，以及与 Lua 聚合结果的逐根比较。

## 环境与命令

```bash
cd tools/akshare_data
uv sync
uv run python fetch_sina_futures.py --symbol RB0 --period 1
uv run pytest
uv run ruff check .
```

默认按 `KLINE_DATA_HOME`、`XDG_DATA_HOME/kline`、`~/.local/share/kline` 的优先级选择共享目录。完整规范见项目根目录的 `DATABASE.md`。

```text
$KLINE_DATA_HOME/
├── raw/
│   └── akshare-sina/RB0/1m/2026/RB0_1m_sina.csv
├── canonical/
│   └── v1/akshare-sina/RB0/1m/2026/RB0_1m_20260814.csv
└── locks/
```

可以指定其他目录：

```bash
uv run python fetch_sina_futures.py \
  --symbol RB0 \
  --period 1 \
  --data-home /tmp/kline-sina-data
```

默认通过 `ak.tool_trade_date_hist_sina()` 推算交易日。如果该接口临时不可用，可以退化为只跳过周末：

```bash
uv run python fetch_sina_futures.py --symbol RB0 --calendar weekday
```

## 字段转换

| 新浪 / AKShare | kline.luajit | 处理方式 |
|---|---|---|
| `datetime` | `bar_time` | 默认视为结束时间，减去一个周期 |
| `open/high/low/close` | 同名字段 | 保留，写 CSV 时按指定精度格式化 |
| `volume` | `volume` | 新浪已经是每根 K 线成交量，不再做累计差分 |
| `hold` | `open_interest` | 直接映射 |
| 无 | `trading_day` | 使用新浪交易日历和夜盘时间推算 |
| 无 | `turnover` | 填 `0` |
| 无 | `settlement` | 填 `0` |
| 无 | `tick_count` | 填 `0`，不能伪造 tick 数量 |
| 无 | `flags` | `CLOSED=1`，夜盘再加 `NIGHT=4` |

规范化文件带 `#` 元信息头，并使用主项目的列顺序：

```csv
# symbol=RB0 period=1m tz=Asia/Shanghai schema=v1 source=akshare/sina source_time=end calendar=sina night_start=18:00 day_start=08:00
bar_time,trading_day,open,high,low,close,volume,turnover,open_interest,settlement,tick_count,flags
```

重复执行抓取时，同一个交易日文件以 `bar_time` 为键更新，不会重复追加。同一时间的新值覆盖旧值，文件最终按时间升序排列。写入者在读取、合并和原子替换全过程持有对应的 `flock`；读取者读取单个文件时不需要加锁。

## 已知差异和注意事项

### 1 分钟线时间戳不一致

`kline.luajit` 使用左闭右开 `[start, end)`，`bar_time` 必须是分钟开始时间；例如收盘前最后一根是 `14:59`。

AKShare 文档中的新浪样例包含 `15:00` 行。2026-08-15 的真实抓取进一步确认，完整交易段返回为：

```text
09:01-10:15  10:31-11:30  13:31-15:00  21:01-23:00
```

每段都比本项目的起始时间标签晚一分钟，因此新浪接口使用结束时间标签。转换默认使用 `--timestamp-mode end`，将 1 分钟数据减 1 分钟，5 分钟数据减 5 分钟。转换后的边界为：

```text
09:00-10:14  10:30-11:29  13:30-14:59  21:00-22:59
```

如果未来新浪接口改变语义，可以改用：

```bash
uv run python fetch_sina_futures.py --symbol RB0 --period 1 --timestamp-mode start
```

原始文件永远保留新浪时间，不受该选项影响，可用于重新转换和排查。
不同 `timestamp-mode` 的规范化结果不能写入同一输出目录；工具会检查文件元信息并拒绝混写。

### 其他差异

- 新浪代码要求大写，例如连续合约 `RB0`；主项目示例通常是具体合约 `rb2510`。连续合约会换月，不应直接当成单一具体合约的长期序列。
- 新浪接口只返回近期有限窗口，不支持任意起止日期。AKShare 文档样例约为 1023 根，实际数量由新浪决定。
- 新浪没有 `turnover`、`settlement` 和 `tick_count`，兼容文件中的零表示“数据源未提供”，不是实际值。
- 新浪不返回期货 `trading_day`。当前使用新浪股票交易日历近似推算；期货交易所节假日安排可能存在差异，CTP 字段仍是权威来源。
- 夜盘默认把 18:00 之后和 08:00 之前标记为 `NIGHT`。特殊品种或交易时间调整需要通过 `--night-start`、`--day-start` 修正。
- 新浪可能出现缺分钟、临时接口失败、字段变化或限流。转换器对缺列、非法数字和重复时间直接报错，不静默生成测试数据。
- 价格和成交量单位沿用新浪返回值；不同交易所、品种及连续合约切换时必须单独核对。

## 开发记录

### 2026-08-15

- 确认 AKShare 1.18.87 的接口返回列为 `datetime/open/high/low/close/volume/hold`。
- 实际抓取 `RB0` 得到 1023 根，范围为 `2026-08-12 09:13` 至 `2026-08-14 23:00`。
- 确认新浪各完整交易段均使用结束时间标签；减一分钟后与主项目的左对齐边界完全一致。
- 周五夜盘数据成功归入下周一 `20260817` 交易日。
- 采用原始数据与规范化数据双份保存，避免时间语义调整时重复请求新浪。
- 规范化文件按 `bar_time` upsert，保持与主项目最新持久化策略一致。
- 生成的 `20260814` 文件已由 LuaJIT `persist.load` 成功加载 345 根。
- 将默认输出切换到 `KLINE_DATA_HOME` 共享目录，并按 source/symbol/period/year 分区。
- 为 raw 年文件和 canonical 交易日文件增加独立 `flock`，锁覆盖读取、合并和原子替换全过程。
- 实际抓取 `LH2611` 共 1023 根，原始范围为 `2026-08-10 10:58` 至 `2026-08-14 15:00`。
- `LH2611` 规范化后得到 5 个交易日文件，共 1023 根且 `bar_time` 无重复、全局有序；`20260814` 文件已由 LuaJIT 成功加载 225 根。

本机数据保存在：

```text
~/.local/share/kline/raw/akshare-sina/LH2611/1m/2026/
~/.local/share/kline/canonical/v1/akshare-sina/LH2611/1m/2026/
```
