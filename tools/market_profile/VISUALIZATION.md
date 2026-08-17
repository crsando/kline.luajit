# Tick、1min K线与 Support/Resistance 可视化

本模块将 `day_market_profile` 的标准化 tick、Profile、level 和 event 产物转换成完全离线的
单品种 HTML、每日总览 HTML 和 PNG 图片。HTML 使用本地 Apache ECharts 5.6.0，不需要
开发服务器，也不需要联网。

> 当前 `20260109` 只有单日 smoke-test 数据。图中的 bounce/break 只是事件标签，不表示
> 统计显著或可交易收益。

## 1. 设计目标

每个详情页只回答三个问题：

1. 当天 tick 路径和 1min K线如何运动？
2. 上午 Profile 识别出的 zone 在下午何时可见、何时被触碰？
3. 触碰后是 bounce、break 还是 timeout？

页面采用同一价格坐标：

```text
1min K线 + Tick path + zones + event markers | Volume Profile
Volume + Open Interest                      | Level list
```

右侧 Volume Profile 与主图共享相同 `priceMin/priceMax`，避免视觉错位。

## 2. 运行

先运行 Profile pipeline，再生成可视化：

```bash
python3 tools/market_profile/market_profile.py \
  ~/Downloads/20260109 \
  ~/Downloads/20260109/day_market_profile \
  --overwrite

python3 tools/market_profile/visualize.py \
  ~/Downloads/20260109/day_market_profile \
  ~/Downloads/20260109/day_market_profile/visual \
  --overwrite
```

只生成 HTML、不生成 PNG：

```bash
python3 tools/market_profile/visualize.py \
  ~/Downloads/20260109/day_market_profile \
  ~/Downloads/20260109/day_market_profile/visual \
  --skip-images --overwrite
```

指定 Chrome：

```bash
python3 tools/market_profile/visualize.py DATA_DIR OUTPUT_DIR \
  --chrome "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --overwrite
```

## 3. 输出结构

```text
visual/
├── index.html
├── manifest.json
├── visual_result.json
├── assets/
│   ├── echarts.min.js
│   └── ECHARTS-LICENSE.txt
├── details/
│   ├── IF2603_20260109.html
│   ├── AP605_20260109.html
│   └── ...
└── images/
    ├── daily_overview.png
    ├── IF2603_20260109.png
    ├── AP605_20260109.png
    └── ...
```

`index.html` 不嵌入全部 tick，只包含轻量 mini SVG 和跳转链接。每个 detail HTML 只嵌入
自身合约数据，因此可直接双击打开，也不会让总览页膨胀。

## 4. 数据映射

### 1min K线

从 `selected_ticks/*.csv.gz` 重新按自然分钟聚合：

```text
open  = 分钟第一笔有效价格
high  = 分钟最高价格
low   = 分钟最低价格
close = 分钟最后价格
volume = sum(delta_volume)
open_interest = 分钟最后值
```

仅用于显示，不重新计算 Profile 或 support/resistance。

### Tick path

详情页内嵌两层 tick 数据：

```text
ticksOverview：每秒保留 first/min/max/last
ticksFull：保留价格变化或 delta_volume > 0 的 tick
```

全日视图默认使用 `ticksOverview`。当 dataZoom 范围缩小到 35% 以下时自动切换到
`ticksFull`。ECharts line series 同时启用 `lttb` sampling 和 progressive rendering。

事件坐标始终来自原始 `events.csv` 的 touch time，不使用 LOD 数据重新判定。

### 休盘

横轴按实际存在的 1min Bar 排列，非交易时段被压缩。任何相邻 Bar 间隔超过 1.5 分钟时，
生成明确的垂直虚线：

```text
短间隔：休盘
大于等于 60 分钟：午休
```

程序不会插入虚假 K 线或连接不存在的分钟。

## 5. Zone 与事件编码

Zone 使用真实 `lower/upper` 绘制半透明区间，不使用假精确水平线。

关键约束：

```text
zone.startX = 第一个 afternoon bar 之前的边界
```

因此上午计算出的 level 只从 `available_time=11:30` 向后显示，不会横贯上午形成视觉
look-ahead。

角色同时使用颜色、线型和文字：

```text
neutral：琥珀色 + 虚线
support：绿色 + 实线 + support 标签
resistance：红色 + 实线 + resistance 标签
```

事件 marker：

```text
bounce：圆形
break：菱形
timeout：三角形
```

侧栏同时显示 node type、zone 范围、role、outcome 和 scale persistence，避免只靠颜色判断。

## 6. Volume Profile

默认显示：

```text
LTP raw horizontal bars
LTP sigma=2 smooth curve
```

页面可切换：

```text
method：LTP / VWAP
sigma：Raw / 1 / 2 / 4 ticks
```

Raw bars 和 smooth curve 使用独立 series，但共享 Profile x-axis 和主图 price y-axis。
页面不会默认叠加全部平滑尺度。

## 7. 交互

详情页支持：

- Tick path 显示/隐藏；
- Zone 显示/隐藏；
- LTP/VWAP 切换；
- sigma 切换；
- 鼠标滚轮缩放和拖动；
- 1min、tick 和 event tooltip；
- 当前 chart 导出 PNG；
- 当前 chart 使用 SVG renderer 导出 SVG。

页面顶部显示 instrument、exchange、tick size、available time、tick 数、1min 数、zone 数和
event 数。

## 8. 静态图片

批量 PNG 不另写 Matplotlib 业务逻辑，而是直接使用 Chrome headless 截取同一 HTML：

```text
detail image：1920 x 1080
daily overview：1920 x 动态高度
```

Chrome 150 在截图完成后可能不主动退出。生成器会轮询 PNG 文件大小，稳定 0.6 秒后回收
独立 headless 进程组。每张图片使用单独 user-data-dir，避免 SingletonLock 竞争。

## 9. Manifest

`manifest.json` 记录：

```text
schema_version
visual_version
trading_day
generated_at
renderer
profile_methods
smoothing_sigmas_ticks
instrument/root/exchange
tick_size
available_time
detail HTML path
PNG path
bars/ticks/zones/events counts
```

这使图片、HTML 和 Profile 参数之间可追溯。

## 10. 离线依赖

ECharts runtime 固定为 `5.6.0`：

```text
tools/market_profile/assets/echarts.min.js
tools/market_profile/assets/ECHARTS-LICENSE.txt
```

生成时复制到 `visual/assets/`。详情页使用相对路径加载，`file://` 下也可运行。

## 11. 测试

```bash
cd tools/market_profile
python3 -m unittest -v
```

可视化测试覆盖：

- 1min OHLCV 聚合；
- 休盘检测；
- Tick LOD 保留 first/min/max/last；
- zone 从 afternoon boundary 开始；
- event 坐标晚于 zone 起点；
- Profile LTP/VWAP 序列化；
- manifest 与独立 HTML 数量；
- 离线 ECharts asset；
- Profile custom series 的 `encode:{x:1,y:0}`。

## 12. 局限

1. HTML 内嵌的是经过语义压缩的 tick，不是每条无变化行情快照。
2. 图片是固定 viewport，交互分析应使用 HTML。
3. 浏览器截图依赖本机 Chrome；没有 Chrome 时可使用 `--skip-images`。
4. 当前图表不提供交易指令、盈亏或策略收益展示，避免把单日事件误解为策略表现。
5. 总览 mini chart 用于扫描，不应代替详情页的精确价格和事件检查。
