# K 线本地共享存储规范

本文定义本机多个项目共享 K 线数据时的目录、格式、并发和生命周期约定。当前阶段使用文件系统作为事实来源，不引入常驻数据库。

## 1. 数据根目录

所有项目统一读取 `KLINE_DATA_HOME`。没有显式设置时，按以下顺序回退：

1. `$KLINE_DATA_HOME`
2. `$XDG_DATA_HOME/kline`
3. `~/.local/share/kline`

推荐在 shell 配置中明确设置：

```bash
export KLINE_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/kline"
```

K 线是持久数据，不放入项目仓库、`/tmp` 或 `~/.cache`。如果只在 Linux/WSL 中使用，优先放在 Linux 文件系统；需要 Windows 原生程序直接读取时，可以把变量指向 `/mnt/d/market-data/kline`，但需要单独验证锁和原子替换语义。

## 2. 目录结构

```text
$KLINE_DATA_HOME/
├── raw/
│   └── akshare-sina/
│       └── LH2611/1m/2026/
│           └── LH2611_1m_sina.csv
├── canonical/
│   └── v1/
│       └── akshare-sina/
│           └── LH2611/1m/2026/
│               └── LH2611_1m_20260814.csv
├── manifests/
├── locks/
└── tmp/
```

- `raw`：保留数据源字段和时间语义，只按源时间去重，不做左对齐或字段补齐。
- `canonical/v1`：转换成 `kline.luajit` 可直接加载的标准 CSV。`v1` 是 schema 版本，不兼容变更必须使用新版本目录。
- `manifests`：预留给批次、来源、行数、校验和及抓取时间记录。
- `locks`：写入者使用的 advisory lock 文件。锁文件长期保留，文件存在不代表当前有人持锁。
- `tmp`：预留给跨目录任务；单文件更新优先在目标文件同目录创建临时文件，以保证原子替换。

路径中的来源、合约、周期和年份必须完整，防止连续合约、具体合约或不同数据源相互覆盖。

## 3. Canonical v1 格式

文件按 `trading_day` 切分，命名为：

```text
{symbol}_{period}_{trading_day}.csv
```

文件内 `(symbol, period, bar_time)` 唯一，按 `bar_time` 升序排列。列顺序固定为：

```text
bar_time,trading_day,open,high,low,close,volume,turnover,open_interest,settlement,tick_count,flags
```

`bar_time` 使用北京时间的 bar 起始时间，区间语义为左闭右开 `[start, end)`。文件第一行必须是 `#` 元信息，至少记录：

```text
symbol period tz schema source source_time calendar
```

同一文件禁止混用不同 schema、数据源或时间戳语义。

## 4. 写入协议

每个目标交易日文件使用独立排他锁，例如：

```text
locks/canonical-v1__akshare-sina__LH2611__1m__20260814.lock
```

所有写入者必须遵循：

```text
获取 flock 排他锁
  -> 读取目标文件
  -> 按 bar_time 合并或修正
  -> 在同目录写临时文件
  -> flush + fsync
  -> os.replace 原子替换
  -> 释放锁
```

锁必须覆盖“读取、合并、替换”全过程。只给最终写入加锁仍会发生 lost update。

进程退出或崩溃时，操作系统会自动释放 `flock`。不要在系统运行期间删除 `.lock` 文件，否则不同进程可能锁住不同 inode。当前协议面向 Linux/WSL 本地文件系统；Windows 原生写入者不应假设能与 WSL `flock` 互通。

## 5. 读取协议

读取单个 canonical CSV 不需要加锁。写入者只原子替换完整文件，因此读者会看到完整旧版本或完整新版本，不会看到半个文件。

以下情况需要共享锁或版本化快照：

- 一次读取多个文件，并要求它们严格属于同一批次。
- CSV 与 manifest 必须保持事务一致。
- 存储位于无法确认 `rename/replace` 语义的网络文件系统。

多文件一致性优先采用版本化快照：先写完整批次目录，最后原子更新 `CURRENT` 指针；读者只读取一次 `CURRENT`。

## 6. 文件系统与数据库边界

当前继续使用文件系统，原因是数据主要用于历史测试、按日回放和跨语言检查，单日分钟数据很小，CSV 可被 LuaJIT、Python、DuckDB 和人工直接读取。

出现以下需求时再迁移：

- 多个进程持续写同一数据集。
- 频繁修正大量历史 bar。
- 经常跨品种、跨周期做任意时间范围查询。
- 需要远程访问、权限、事务或服务化写入。

本地事务型存储优先考虑 SQLite WAL；分析查询优先使用 Parquet + DuckDB；跨机器、多写入者使用 PostgreSQL。即使引入数据库，`raw` 原始数据仍建议保留在文件系统。

## 7. AKShare 下载

新浪期货数据工具默认遵循本规范：

```bash
cd tools/akshare_data
uv sync
uv run python fetch_sina_futures.py --symbol LH2611 --period 1
```

可以临时指定另一数据根目录：

```bash
uv run python fetch_sina_futures.py \
  --symbol LH2611 \
  --period 1 \
  --data-home /path/to/kline
```

详细的数据源限制和字段差异见 `tools/akshare_data/README.md`。
