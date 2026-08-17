# 纯日盘 Tick Volume Profile

本工具从 CTP 风格的累计行情快照中构建纯日盘 Tick Volume Profile，并使用上午冻结的
Profile level 对下午行情做 first-touch smoke test。当前版本只依赖 Python 3.9 标准库，
不需要安装 NumPy、SciPy、Polars 或 Plotly。

> 当前只有 20260109 一个交易日。输出用于验证数据处理、节点识别和事件定义，不构成
> 统计显著性结论，不代表策略收益，也不是交易建议。

## 1. 数据范围

版本化品种、交易时段、 tick_size 和 contract_multiplier 位于
[contracts.json](contracts.json)。第一版只包含确认没有夜盘的品种：

| 交易所 | 品种 |
|---|---|
| CFFEX | IF IC IH IM T TF TS TL |
| DCE | BB FB JD LH LG |
| CZCE | AP CJ JR LR PK PM RI RS SF SM WH |
| SHFE/INE | WR EC |
| GFEX | SI LC PS PT PD |

PL 和 RR 被显式排除，因为 20260109 源文件在 21:00 后存在真实累计成交量增长。
未知品种不会回退到默认交易时段。

每个品种每天排除名称含“连续”的文件，再严格只按上午有效时段内的
sum(delta_volume) 选择一个具体代表合约。代表合约选择和流动性过滤都不读取下午结果。
默认流动性条件：

```text
total_volume >= 1000
nonzero delta-volume updates >= 1000
active-minute coverage >= 60%
unique traded price bins >= 8
```

未通过的品种仍写入 selected_contracts.csv，并给出 exclusion_reasons。

## 2. 交易时段

所有时段均为左闭右开：

| 组别 | 上午构造 Profile | 下午评估 |
|---|---|---|
| 商品 | 09:00-10:15, 10:30-11:30 | 13:30-15:00 |
| 股指 | 09:30-11:30 | 13:00-15:00 |
| 国债 | 09:30-11:30 | 13:00-15:15 |

上午 level 的 available_time 固定为 11:30:00。事件评估会断言每条下午 tick 都晚于
available_time，防止 look-ahead。

## 3. Tick 标准化

源 Volume 和 Turnover 是累计值。只对已经通过价格、时间顺序和交易时段检查的 tick
执行差分：

```text
delta_volume   = max(volume[t] - volume[t-1], 0)
delta_turnover = max(turnover[t] - turnover[t-1], 0)
```

第一笔有效 tick 只建立基准，delta 为 0。计数器回退、长间隔和异常 VWAP 都写入
quality_flags，不会静默忽略。

标准化结果位于 selected_ticks/*.csv.gz，字段如下：

```text
trading_day,instrument,event_time,millis_of_day,session,
last_price,volume,turnover,delta_volume,delta_turnover,
interval_vwap,open_interest,gap_millis,quality_flags
```

## 4. 两种 Volume Profile

LTP Profile：

```text
profile[round_to_tick(LastPrice)] += delta_volume
```

Interval-VWAP Profile：

```text
interval_vwap = delta_turnover / (delta_volume * turnover_divisor)
profile[round_to_tick(interval_vwap)] += delta_volume
```

多数交易所的 turnover_divisor 等于 contract multiplier。郑商所这批数据的 Turnover
口径已经按价格归一，因此对应品种在 contracts.json 中显式设置 turnover_divisor=1。

如果 interval VWAP 非有限、非正或距 LastPrice 超过配置阈值，该段 volume 回退到
LastPrice，并标记 VWAP_FALLBACK_LTP。这样 LTP 和 VWAP Profile 均严格保持成交量守恒，
同时异常比例可由 normalization_report.csv 审计。

## 5. Raw Profile 与平滑

价格 bin 始终为一个 tick_size。程序保留 raw Profile，并生成：

```text
sigma = 0, 1, 2, 4 ticks
```

Gaussian convolution 之后会重新归一化，保证每个尺度的总 volume 与 raw 完全一致。
平滑只用于识别结构；标准 VPOC、VAH 和 VAL 始终从 LTP raw Profile 计算。

Value Area 默认覆盖 70% volume，从 raw VPOC 向两侧按相邻 bin volume 扩展。若 VPOC
是连续 plateau，使用 plateau 的 volume-weighted center，并保留完整 plateau zone。

## 6. HVN、LVN 与 Zone

每个尺度先根据局部 peak、prominence 和 half-prominence width 找候选 HVN。候选必须：

```text
至少在 3 个 smoothing scale 存在
prominence share >= 0.25% total profile volume
LTP 与 VWAP Profile 中距离 <= 2 ticks
```

LVN 只在相邻 persistent HVN 之间寻找，并要求：

```text
LTP/VWAP valley 距离 <= 2 ticks
valley depth >= 25%
```

最终候选包括 VPOC、VAH、VAL、persistent HVN、persistent LVN 和 profile edges。
重叠或相邻 zone 会合并，node_types 使用 + 保留 confluence 来源。输出是上下边界区间，
不是假精确的单一价格线。

## 7. 下午事件定义

每个冻结 zone 在下午只统计第一次从区间外进入的 touch：

```text
从上方进入 -> candidate support
从下方进入 -> candidate resistance
```

反应和穿透距离根据上午 1min median range 自适应：

```text
reaction_ticks    = max(2, round(0.50 * median_range_ticks))
penetration_ticks = max(1, round(0.25 * median_range_ticks))
```

support 先向上达到 reaction threshold 为 bounce，先向下达到 penetration threshold 为
break；resistance 反向处理。直到下午收盘仍未触发则为 timeout。事件同时保存 MFE、MAE
和 resolution time。

这只是 level 行为标签，不包含手续费、滑点、下单延迟或完整交易规则。

## 8. 运行

从项目根目录执行：

```bash
python3 tools/market_profile/market_profile.py \
  ~/Downloads/20260109 \
  ~/Downloads/20260109/day_market_profile \
  --overwrite
```

输出目录非空时必须显式传 --overwrite。程序只读源 CSV，正式产物使用临时文件和
os.replace 发布。

可视化生成器、离线 HTML、交互编码和 Chrome PNG 导出详见 [VISUALIZATION.md](VISUALIZATION.md)。

运行测试：

```bash
cd tools/market_profile
python3 -m unittest -v
```

项目原有 Lua 测试：

```bash
./run_tests.sh
```

## 9. 输出文件

| 文件 | 内容 |
|---|---|
| selected_contracts.csv | 每品种代表合约、流动性指标和排除原因 |
| selected_ticks/*.csv.gz | 选中合约的标准化纯日盘 tick |
| normalization_report.csv | reset、长间隔、VWAP fallback 等质量统计 |
| profiles.csv | LTP/VWAP 在所有 sigma 下的逐价格 bin profile |
| levels.csv | 上午冻结的 zone 及完整结构特征 |
| events.csv | 下午 first-touch bounce/break/timeout 事件 |
| summary.csv | 按 node type 汇总的 smoke-test 结果 |
| report.html | 自包含、无需服务端的可视化报告 |
| run_config.json | 本次运行使用的完整配置快照 |
| result.json | 总体计数摘要 |

## 10. 20260109 smoke-test 结果

最终版本严格使用上午数据选择代表合约和构造 level：

```text
selected contracts: 21
profile groups: 168 (21 contracts * 2 methods * 4 scales)
frozen zones: 117
afternoon first-touch events: 68
bounce: 36
break: 31
timeout: 1
```

LG 因上午 nonzero delta-volume updates 不足 1000 被流动性过滤排除；其余低流动性排除项
和原因见 selected_contracts.csv。上面的 36/31 不能解释为 52.9% 的稳定胜率：节点类型、
品种、zone 宽度和反应阈值不同，而且只有单日样本。它只是确认完整流程能以 point-in-time
方式产生并解析事件。

## 11. 已知限制

1. 原始数据是约 500ms 行情快照，不是真正逐笔成交；一个间隔内的多笔成交只能用
   LastPrice 或 interval VWAP 近似分配。
2. 当前只有一个交易日，不能估计稳定 bounce probability，也不能做显著性检验。
3. 上午 Profile 对下午的测试样本不是跨日独立样本，只适合 smoke test。
4. 代表合约按当日上午 volume 选择，只适用于 11:30 后生成的 level；如果要在上午盘中
   运行，必须改用前一日主力规则。
5. metadata 有 effective_date。交易所修改 tick size、multiplier 或交易时段时必须新增版本，
   不能覆盖历史口径。
6. 合并后的 confluence node_types 可能很长；后续多日研究应保留原始节点关系并单独建模。
7. 当前没有 matched random level、shuffled return、手续费和 multiple-testing correction。

## 12. 下一阶段

获得多日数据后，应按 trading_day 做 walk-forward：D 日完整 Profile 只用于 D+1，或者使用
盘中 Developing Profile 的 point-in-time 快照。需要加入 matched random levels、按品种和
交易日 clustered bootstrap，以及参数网格的多重检验修正，再讨论 level 是否真正有效。
