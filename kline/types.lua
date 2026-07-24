-- kline/types.lua —— FFI 类型定义:Bar struct、周期与位标志枚举
-- 字段顺序经过重排:所有 double 落在 8 字节对齐边界,无内部 padding 空洞,
-- sizeof(kline_bar_t) == 88(末尾 7 字节填充凑成 8 的倍数,保证数组元素对齐)。
local ffi = require("ffi")

ffi.cdef[[
typedef struct {
    int64_t  bar_time;       /* +0   bar 起始时间,Unix 毫秒(时段轴对齐) */
    double   open;           /* +8   开盘价 */
    double   high;           /* +16  最高价 */
    double   low;            /* +24  最低价 */
    double   close;          /* +32  收盘价 */
    double   volume;         /* +40  成交量(差值累加) */
    double   turnover;       /* +48  成交额 */
    double   open_interest;  /* +56  持仓量(时点值,取最新) */
    double   settlement;     /* +64  结算价(日线才有意义,分钟线置 0) */
    int32_t  trading_day;    /* +72  交易日 YYYYMMDD(夜盘归属单独存) */
    int32_t  tick_count;     /* +76  本 bar 累计 tick 数(数据质量/调试) */
    uint8_t  flags;          /* +80  位标志:bit0=已封口 bit1=集合竞价 bit2=夜盘 */
    uint8_t  _pad[7];        /* +81  对齐填充 -> sizeof = 88 */
} kline_bar_t;
]]

local M = {}

-- 常用 ctype(缓存,避免重复解析)
M.bar_t       = ffi.typeof("kline_bar_t")
M.bar_ptr_t   = ffi.typeof("kline_bar_t *")
M.bar_array_t = ffi.typeof("kline_bar_t[?]")
M.SIZEOF_BAR  = ffi.sizeof("kline_bar_t")   -- 应为 88

-- 周期定义:统一用"分钟数"表示;日线用 1440(一天分钟数)作为哨兵值
M.PERIOD = {
  M1   = 1,
  M5   = 5,
  M15  = 15,
  M30  = 30,
  H1   = 60,
  H2   = 120,
  H4   = 240,
  D1   = 1440,   -- 日线(按 trading_day 聚合,特殊处理)
}

-- 周期名 -> 分钟数 的反查(用于配置/落地文件名)
M.PERIOD_NAME = {
  [1]="1m", [5]="5m", [15]="15m", [30]="30m",
  [60]="1h", [120]="2h", [240]="4h", [1440]="1d",
}

-- flags 位标志
M.FLAG = {
  CLOSED  = 0x01,   -- 已封口
  AUCTION = 0x02,   -- 集合竞价 bar
  NIGHT   = 0x04,   -- 夜盘 bar
}

return M
