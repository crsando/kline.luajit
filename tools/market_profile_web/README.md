# Market Profile 本地研究工作台

本地 Node.js 网站，用于扫描按交易日保存的 Tick 数据、运行现有 Python pipeline、浏览
ECharts 结果并记录人工 support/resistance Review。

网站只监听 `127.0.0.1`，不上传数据，也不重新实现 Profile 算法。

## 功能

- 扫描 `YYYYMMDD` 交易日目录；
- 展示 raw tick、1min、Profile、Visual 和 Review 状态；
- 嵌入现有 `visual/index.html` 与单品种详情页；
- 浏览每个 instrument 的 zones、role 和自动 outcome；
- 保存 A/B/C/D 等级、行为标签、Profile/Zone 质量与评论；
- SQLite 持久化 Review；
- 导出每日 Review CSV；
- 分步或串行运行 1min、Profile、Visual pipeline；
- 通过 SSE 实时显示 stdout/stderr 和退出状态；
- 阻止同一交易日重复并发任务。

## 技术结构

```text
Browser
  ↓
原生 HTML/CSS/JavaScript
  ↓
Fastify + TypeScript
  ├── 受控目录扫描
  ├── Pipeline TaskManager
  ├── SSE 日志
  └── Review API
  ↓
Node 24 内置 node:sqlite
  ↓
现有 Python pipeline 与 visual HTML
```

Node.js 负责研究工作流，Python 继续负责：

```text
Tick 清洗
1min 聚合
Volume Profile
Level/Event 识别
HTML/PNG 生成
```

## 环境

```text
Node.js >= 24
Python 3.9+
```

本机使用 NVM：

```bash
cd tools/market_profile_web
nvm use
```

## 安装

```bash
cd tools/market_profile_web
npm install
```

## 启动

开发模式：

```bash
npm run dev
```

生产构建：

```bash
npm run build
npm start
```

默认地址：

```text
http://127.0.0.1:4317
```

## 配置

通过环境变量配置：

```text
KLINE_HOST         默认 127.0.0.1，只允许 127.0.0.1/localhost
KLINE_PORT         默认 4317
KLINE_DATA_ROOT    默认 ~/Downloads
KLINE_REPO_ROOT    默认自动定位当前仓库
KLINE_REVIEW_DB    默认 ~/Library/Application Support/kline-market-profile/reviews.sqlite
KLINE_PYTHON       默认 python3
```

示例：

```bash
KLINE_DATA_ROOT="$HOME/Downloads" \
KLINE_PORT=4317 \
npm start
```

## 数据目录

网站只扫描数据根目录下名称符合 `YYYYMMDD` 的直接子目录：

```text
~/Downloads/
├── 20260109/
│   ├── *.csv
│   ├── 1min/
│   └── day_market_profile/
└── 20260112/
```

任意路径参数都经过 root containment 检查。Visual 路由只允许：

```text
.html .js .css .json .png .svg .txt
```

不提供原始 Tick 的任意文件下载 API。

## Pipeline

网站使用 `child_process.spawn(command, args)`，不经过 shell。

### 1min

```bash
python3 tools/tick_data/convert_to_1m.py \
  DAY_DIR DAY_DIR/1min --workers 8
```

### Profile

```bash
python3 tools/market_profile/market_profile.py \
  DAY_DIR DAY_DIR/day_market_profile --overwrite
```

### Visual

```bash
python3 tools/market_profile/visualize.py \
  DAY_DIR/day_market_profile \
  DAY_DIR/day_market_profile/visual \
  --overwrite
```

“运行全部”严格按 `1min → profile → visual` 顺序。任一步非 0 退出都会停止后续步骤。

## 人工 Review

Review 与源 CSV 分离，保存在 SQLite：

```text
trading_day
instrument
zone_id
node_type
center/lower/upper
auto_outcome
manual_grade
manual_behavior
profile_quality
zone_quality
comment
created_at/updated_at
```

`zone_id` 由以下字段计算稳定 SHA-256 前缀：

```text
trading_day | instrument | node_type | lower | upper | available_time
```

等级：

```text
A 清晰
B 一般
C 勉强
D 不合理
```

行为：

```text
clean_bounce
weak_bounce
immediate_break
choppy
no_touch
data_problem
```

## API

```text
GET  /api/health
GET  /api/config
GET  /api/days
GET  /api/days/:date
GET  /api/days/:date/instruments
GET  /api/days/:date/instruments/:instrument/levels
PUT  /api/days/:date/reviews/:zoneId
DELETE /api/days/:date/reviews/:zoneId
GET  /api/days/:date/reviews.csv
POST /api/days/:date/run
GET  /api/days/:date/tasks
GET  /api/tasks/:id
GET  /api/tasks/:id/events
GET  /data/:date/visual/*
```

启动任务：

```json
{"step":"1min"}
{"step":"profile"}
{"step":"visual"}
{"step":"all"}
```

## 测试

```bash
npm test
```

测试覆盖：

- 日期和路径穿越防护；
- SQLite Review upsert 与 CSV 导出；
- 交易日状态扫描；
- Manifest、instrument 和 zone 读取；
- API Review CRUD 与受控 Visual 文件；
- Pipeline 顺序执行；
- 同一日期并发任务冲突；
- 非法日期和非法 pipeline step。

TypeScript 检查：

```bash
npm run check
```

## 当前边界

- 单用户、仅本机使用；
- 任务状态保存在内存，服务重启后历史任务日志不会保留；
- Review 持久化到 SQLite，不受服务重启影响；
- 不做策略回测、交易下单或账户管理；
- 不自动修改 Profile 参数；
- 当前 UI 以每天重复运行和肉眼 Review 为核心。
