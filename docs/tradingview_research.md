# TradingView K 线聚合逻辑调研

> 调研目标：搞清 TradingView 的多周期 K 线是怎么对齐/聚合的，与国内期货（vnpy/文华）逻辑对比，
> 为自研 LuaJIT K 线库的对齐策略提供参考。
> 调研日期：2026-07-23
> 说明：TradingView 未公开完整合成源码。以下基于**官方 Pine Script 文档**（可靠）+ **大量社区脚本/讨论中反复验证的行为**（事实层面可信，但非官方逐字定义），已在文末标注来源与置信度。

---

## 一、核心结论（一句话）

**TradingView 走的是"从交易所日边界（午夜）向下取整"的纯墙上时钟对齐，不做 session 感知的强制封口。**
所以高周期 bar 会"吃掉"开盘时刻——**美股 1 小时图上根本没有独立的 09:30 bar**，开盘被并进 09:00–10:00 那根。
这与国内期货（文华/vnpy）"从 session 起点对齐 + 休盘强制封口"是**相反的哲学**。

---

## 二、Timeframe（周期）规范（官方，确定）

来自官方 Pine Script 文档，很能反映其内部模型：
- 周期字符串 = 倍数 + 单位：`"1S"`、`"30"`(30分钟)、`"1D"`、`"3M"`(3个月)。
- 单位字母：`T`=tick、`S`=秒、`D`=日、`W`=周、`M`=月；**分钟没有字母**（`"60"` 就是 60 分钟）。
- **没有"小时"单位**：`"1H"` 非法，1 小时必须写 `"60"`。→ 说明内部把小时也当分钟数处理。
- 合法倍数：
  - tick：仅 1/10/100/1000
  - 秒：仅 1/5/10/15/30/45
  - **分钟：1–1440**（所以能做 45、90 分钟等"非整除 60"的周期！）
  - 日 1–365、周 1–52、月 1–12
- 对比 vnpy：vnpy 原生只支持能整除 60 的分钟窗口（2/3/5/6/10/15/20/30）；**TradingView 支持 1–1440 任意分钟数**，这点更灵活。

---

## 三、bar 对齐锚点（关键差异，社区高置信度）

### 3.1 日内高周期从"日边界"对齐，而非 session 开盘
多个独立社区来源反复指出同一现象：
> "On 1h charts bar alignment can cut off session opens (there is no 09:30 bar on an hourly chart)."
> "Hourly bars cut off the 09:30 New York open."

含义：美股 regular session 09:30 开盘，但 60 分钟图的 bar 边界是 …08:00、09:00、10:00…（从交易所时区午夜/整点向下取整），
所以 **09:30 的开盘落在 09:00–10:00 这根里，没有一根以 09:30 为起点的独立小时 bar**。

- 45 分钟、90 分钟等非标周期同理：从日边界按固定步长切，不迁就 session 起点。
- 这就是"墙上时钟对齐"的纯粹形态：`bar_start = floor((t - day_anchor) / period) * period + day_anchor`。

### 3.2 为什么 TradingView 这么设计？
- 它是**全球全品种平台**（股/期/外汇/加密，成千上万标的），
  无法为每个品种维护精确的 session 时段表来做 session 对齐 → 干脆用统一的日边界对齐，**简单、一致、可预测**。
- 代价：牺牲了 session 边界的精确性（开盘/收盘 bar 会被"截断"或合并），
  这也是为什么很多 session 类指标**在 1h 以上直接拒绝工作**（作者注："Above 1h the script draws nothing on purpose"）。

### 3.3 时间戳标注：起始时间（左对齐）
Pine 里 `time` 返回 bar 的**开盘时间**，`time_close` 返回收盘时间。bar 以**起始时间**标注（左闭），与国内约定一致。

---

## 四、日/周/月线 与 session 处理

- **日线**按交易所交易日聚合（交易所日历决定，含节假日）——数据侧由 TradingView/数据商保证，用户不用自己算。
- **周/月线**由日线聚合。
- **session（含盘前盘后）**：TradingView 用 session 规格串（如 `"0930-1600"`）描述常规时段，但这主要用于**着色/时段判定**，
  并**不改变高周期 bar 的对齐锚点**（对齐仍走日边界）。
- **隔夜 gap**：不补空 bar，非交易时段没有 bar（和国内一致）。

---

## 五、Pine 多周期数据的坑（request.security）

自研如果参考其 API 设计，这几个坑要知道：
- **repaint / lookahead**：`request.security()` 用 `lookahead=barmerge.lookahead_on` 会取到"未来"高周期数据，回测虚高、实盘打脸。正确做法是默认 `lookahead_off` + 用已确认 bar。
- **未确认 bar 抖动**：实时最后一根高周期 bar 未收盘时值会变，策略信号应基于**已确认 bar**（类似国内用倒数第二根 `[-2]`）。
- **非标准图表**（Renko/Volume/Range/Kagi）：不是按时间聚合，`request.security` 在其上取值有专门警告（"Backtesting on Non-Standard Charts: Caution!"）。

---

## 六、与国内期货逻辑对比（对自研库的启示）⭐

| 维度 | TradingView | 国内期货（文华/vnpy/本设计） |
|---|---|---|
| 对齐锚点 | **日边界（午夜/整点）向下取整** | **session 起点对齐** |
| 开盘处理 | 会"吃掉"开盘（无独立开盘 bar） | 开盘 bar 从时段起点干净开始 |
| 休盘/午休 | 不做 session 强制封口 | **休盘边界强制封口** |
| 非整除周期 | 原生支持 1–1440 分钟 | vnpy 原生不支持；需交易时长对齐 |
| session 表 | 不为每品种维护（全球通用） | 必须维护品种时段表 |
| 设计取向 | 通用、简单、可预测 | 贴合期货交易时段、精确 |

**给自研 LuaJIT 库的决策**：
1. 你面向**国内期货专用**，应坚持 **session 起点对齐 + 休盘强制封口**（比 TradingView 精确，贴合交易习惯，和文华一致）。
2. 但可借鉴 TradingView 两点：
   - **支持 1–1440 任意分钟周期**（用交易时长对齐即可做 45/90 分钟，见 hxxjava 方案）。
   - **API 层区分"已确认 bar / 未确认 bar"**，回调只在封口时触发，避免 repaint 式抖动。
3. 若未来要做**跨品种/全球标的**的通用聚合，再考虑 TradingView 式的日边界对齐作为可选模式。

---

## 来源与置信度

- **官方 Pine Script 文档 · Timeframes**（周期规范，高置信）：https://www.tradingview.com/pine-script-docs/concepts/timeframes/
- **官方 Pine Script 文档 · Other timeframes and data / request.security**（多周期与 repaint）：https://www.tradingview.com/pine-script-docs/concepts/other-timeframes-and-data/
- 社区多来源验证"1h 无 09:30 bar / 小时 bar 截断开盘"（行为层高置信，非官方逐字）：
  - https://tw.tradingview.com/scripts/sessions/
  - https://tw.tradingview.com/scripts/%23newyorksession/
- 非标准图表回测警告（PineCoders）：https://www.tradingview.com/scripts/pinecoders/

> 备注：TradingView 官方 Help Center 未提供"高周期 bar 逐字对齐算法"的公开文档，
> 日边界对齐是社区长期观察到的一致行为，可信但属经验性结论；如需 100% 确证，建议在其平台上实测（美股 60 分钟图看是否存在 09:30 bar）。
