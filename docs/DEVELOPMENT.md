# kline.lua — LuaJIT 期货 K 线内存库 · 开发计划

> **进度（2026-07-24）：M0–M5 全部完成 ✅　10 个 spec、192 断言全绿。**
> tick → 1m 合成 → 5m/15m 派生 → CSV 落地 → 加载回放，端到端跑通（LuaJIT 2.1 / WSL）。
> 跑测试：`wsl bash -c "cd .../outputs && bash run_tests.sh"`


> 本文件是**面向编码实现**的工作计划（不是设计稿）。设计细节见 `kline_lib_design.md`，
> 平台调研与踩坑见 `kline_platforms_research.md`。
> 目标：按里程碑把库从 0 做到可实盘接入，每步都有明确产出与验收。

---

## 0. 技术栈与约定

- **运行时**：LuaJIT 2.1（FFI）。纯 Lua 依赖尽量少。
- **测试**：**纯 Lua 脚本 + 极简断言 helper**（`spec/t.lua`，零依赖，不用 busted）+ 自制回放脚本（用真实 1m CSV 喂数据）。
- **构建/跑测**：`luajit`；跑测试 `for f in spec/*_spec.lua; do luajit "$f" || exit 1; done`（失败自动非零退出码）。
- **落地互操作验证**：Python + pandas/DuckDB（读 CSV/Parquet 校验）。用 `uv run --with pandas ...`。
- **全局铁律**（贯穿所有模块，来自 vnpy 踩坑）：
  1. bar 时间区间语义**全库统一左闭右开 `[start, end)`**。
  2. bar 时间戳一律用**起始时间**（不是结束时间）。
  3. **volume 用差值累加**（CTP volume 是当日累计值）。
  4. 休盘 gap **不补空 bar**；缺失的 1m 直接跳过。
  5. 封口要**主动触发**（时段/收盘边界），不纯靠下一根数据推动。

---

## 1. 模块开发顺序与依赖

```
M0 基础          M1 内存表         M2 落地          M3 时段         M4 合成         M5 派生+集成
─────────        ─────────         ─────────        ─────────       ─────────       ─────────
types.lua   ──▶  series.lua   ──▶  codec.lua   ──▶  calendar.lua──▶ aggregator ──▶ period(rollup)
util.lua         store.lua         persist.lua      config.lua      (tick→1m)      init.lua(聚合API)
bar.lua                                                             + 主动封口      端到端回放
```

**关键路径**：M0 → M1 是地基，必须先稳。M2（落地）和 M3（时段）可并行。M4 依赖 M1+M3。M5 收口。

---

## 2. 里程碑与任务清单

### 🧱 M0 — 基础类型（1–2 天）
产出：能定义 Bar、能新建/读写单根 bar。
- [ ] `types.lua`：`ffi.cdef` 定义 `kline_bar_t`（88 字节定长，字段见设计稿第 2 节）+ period/flags 枚举常量。
- [ ] `bar.lua`：`new()` / `to_table()` / `from_table()` / `copy()` / 位标志读写。
- [ ] `util.lua`：毫秒时间戳 ↔ 可读字符串、当日分钟数、日志、错误封装。
- **验收**：`sizeof(kline_bar_t) == 88`；建 bar、读写字段、位标志正确；单测通过。

### 🗂️ M1 — 内存时间序列（2–3 天）· 核心
产出：能在内存里维护一条 (symbol,period) 序列，支持追加/查询/二分。
- [ ] `series.lua`：预分配 FFI 数组 + 倍增扩容；`append` / `last` / `at`（支持负索引）/ `count` / `slice(from,to)` / `find(bar_time)`（二分）。Series header 存 symbol/period。
- [ ] `store.lua`：多 Series 容器，`series(sym,period)` 建/取、遍历。
- **验收**：插入 10 万根 bar 无明显 GC 抖动；二分定位正确；扩容后数据完整；slice 返回视图不拷贝。

### 💾 M2 — 落地与加载（2–3 天）
产出：内存序列能增量写 CSV、能从 CSV 读回。
- [ ] `codec.lua`：CSV 后端——写 `#` 元信息头 + 列名 + 数据行；价格定点格式化；读回解析。预留 JSONL 后端接口。
- [ ] `persist.lua`：按 `bar_time` 主键幂等 upsert；文件切分 `{symbol}_{period}_{trading_day}.csv`；同目录临时文件原子替换；`load()` 预热。
- [ ] **互操作验收脚本**：Python 用 `pd.read_csv(comment='#', parse_dates=['bar_time'])` 读回，校验行数/OHLC 一致。
- [ ] Parquet 归档脚本（DuckDB `COPY ... TO parquet`），独立于 Lua。
- **验收**：写→读回 round-trip 数据一致；pandas 能无警告读入；重启恢复后重复落地不增行；同时间修正可覆盖。

### 📅 M3 — 交易时段与日历（2–3 天）
产出：能判定时段、算 trading_day、判断封口边界。
- [ ] `config.lua`：品种→交易时段字符串配置，如 `"21:00-23:00,09:00-10:15,10:30-11:30,13:30-15:00"`。
- [ ] `calendar.lua`：解析时段字符串（借鉴 hxxjava TradingHours）；`in_session(t)`；`is_session_end(t)`；`is_last_min_of_segment(t)`（= `in_session(t) and not in_session(t+1min)`）；`trading_day(t)`（优先用 CTP 字段，日历做 fallback；夜盘归属下一交易日、周一跨周末）。
- **验收**：螺纹钢时段下，10:14→封口、11:29→封口、夜盘 22:30 的 trading_day = 次日、周五夜盘 = 下周一；脏 tick（休盘时刻）被判非法。

### ⚙️ M4 — Tick 合成 1m（2–3 天）
产出：喂 tick 实时合成 1m bar，时段感知 + 主动封口。
- [ ] `aggregator.lua`：`on_tick(tick)` 状态机——脏 tick 过滤 → 对齐 → 更新 OHLC → **volume 差值累加** → open_interest 取时点值；跨分钟/命中收盘边界主动封口；`on_bar_closed(cb)` 回调；`flush_open_bars()` 收盘强制封口。
- **验收**：用真实/模拟 tick 回放，合成的 1m 与基准一致；收盘最后一根不丢；无成交分钟不产生空 bar；volume 不爆表。

### 🔗 M5 — 多周期派生 + 集成（3–4 天）
产出：1m→5m/15m/30m/60m/日线 roll-up，端到端可用。
- [ ] `period.lua`：时钟对齐 `align` + 休盘强制封口的 `Rollup`（已有 Python 原型 `work/rollup_demo.py`，翻成 Lua）；日线按 trading_day 聚合；派生关系配置表。
- [ ] `init.lua`：聚合对外 API（`kline.new` / `store:on_tick` / `series:persist` / `on_bar_closed` 等）。
- [ ] **端到端回放**：加载一天 1m → 派生 5m/15m/日线 → 落地 → Python 校验（含 10:15 休盘、11:30 午休、夜盘边界）。
- **验收**：225 根 1m 日盘 → 45 根 5m / 15 根 15m，休盘边界干净封口；日线开盘价=夜盘开盘价；与文华/vnpy 结果对齐。

---

## 3. 测试策略

| 层级 | 内容 | 工具 |
|---|---|---|
| 单元 | 每模块函数（扩容、二分、对齐、时段判定） | busted |
| 集成 | tick→1m→多周期 全链路回放 | 自制回放脚本 + 真实 1m CSV |
| 互操作 | 落地文件 Python 能读、数值一致 | pandas / DuckDB |
| 边界 | 10:15休盘、11:30午休、夜盘跨日、收盘最后一根、无成交分钟 | 构造专项用例 |

---

## 4. 避坑 Checklist（编码时逐条对照）

- [ ] 区间语义全库**左闭右开 `[start,end)`**（vnpy 坑2：小时线多算一根）
- [ ] bar 时间戳用**起始时间**
- [ ] **volume 差值累加**，别直接加 CTP 累计值
- [ ] 休盘/午休/收盘**主动封口**，`in_session(t) and not in_session(t+1)`（vnpy 坑1）
- [ ] 缺失 1m **不补空 bar**
- [ ] 收盘调 `flush_open_bars()`，别让最后一根丢（vnpy 坑4）
- [ ] `trading_day` 优先用 CTP 字段，夜盘归属下一交易日
- [ ] 脏 tick 过滤：`price==0`、时间倒退、非交易时段
- [ ] 不处理长假对齐（只处理周末 + 日历兜底）
- [ ] 5m/15m 只从 1m 派生，1m 是唯一真相源

---

## 5. 目录结构（开发期）

```
kline-lib/
├── README.md              ← 本文件
├── kline/                 ← 库源码（12 个模块）
├── spec/                  ← 纯 Lua 单元测试（t.lua 断言 helper + *_spec.lua）
├── tools/
│   ├── replay.lua         ← 回放脚本
│   ├── verify_csv.py      ← Python 互操作校验
│   └── csv_to_parquet.sql ← DuckDB 归档
├── data/                  ← 测试用 1m CSV 样本
└── docs/
    ├── kline_lib_design.md
    └── kline_platforms_research.md
```

---

## 6. 排期概览

| 里程碑 | 内容 | 预估 | 依赖 |
|---|---|---|---|
| M0 | 基础类型 | 1–2d | — |
| M1 | 内存序列 | 2–3d | M0 |
| M2 | 落地/加载 | 2–3d | M1 |
| M3 | 时段/日历 | 2–3d | M0（可与 M2 并行） |
| M4 | Tick 合成 | 2–3d | M1+M3 |
| M5 | 派生+集成 | 3–4d | M2+M4 |

**总计约 2.5–3 周**（单人、含测试）。MVP 若只要"内存表+CSV 落地+1m 合成"，M0→M1→M2→M3→M4 约 2 周即可跑通。
