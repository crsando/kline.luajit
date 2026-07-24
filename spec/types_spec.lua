-- spec/types_spec.lua —— 验证 Bar struct 内存布局与枚举
package.path = "./?.lua;" .. package.path
local ffi = require("ffi")
local T = require("spec.t")
local types = require("kline.types")

T.suite("types")

-- sizeof 必须是 88(自然对齐,数组元素无 gap)
T.eq(types.SIZEOF_BAR, 88, "sizeof(kline_bar_t)==88")

-- 关键字段 offset(跨语言解析 Node Buffer / Python 时按此表)
T.eq(ffi.offsetof("kline_bar_t", "bar_time"),      0,  "offset bar_time")
T.eq(ffi.offsetof("kline_bar_t", "open"),          8,  "offset open")
T.eq(ffi.offsetof("kline_bar_t", "high"),          16, "offset high")
T.eq(ffi.offsetof("kline_bar_t", "low"),           24, "offset low")
T.eq(ffi.offsetof("kline_bar_t", "close"),         32, "offset close")
T.eq(ffi.offsetof("kline_bar_t", "volume"),        40, "offset volume")
T.eq(ffi.offsetof("kline_bar_t", "turnover"),      48, "offset turnover")
T.eq(ffi.offsetof("kline_bar_t", "open_interest"), 56, "offset open_interest")
T.eq(ffi.offsetof("kline_bar_t", "settlement"),    64, "offset settlement")
T.eq(ffi.offsetof("kline_bar_t", "trading_day"),   72, "offset trading_day")
T.eq(ffi.offsetof("kline_bar_t", "tick_count"),    76, "offset tick_count")
T.eq(ffi.offsetof("kline_bar_t", "flags"),         80, "offset flags")

-- 枚举
T.eq(types.PERIOD.M15, 15, "period M15")
T.eq(types.PERIOD.D1, 1440, "period D1")
T.eq(types.PERIOD_NAME[60], "1h", "period name 1h")
T.eq(types.FLAG.CLOSED, 1, "flag CLOSED")
T.eq(types.FLAG.NIGHT, 4, "flag NIGHT")

-- 数组分配:第 i 个元素地址间隔 = 88
local arr = types.bar_array_t(4)
local base = ffi.cast("char *", arr)
local a0 = ffi.cast("char *", arr + 0)
local a1 = ffi.cast("char *", arr + 1)
T.eq(tonumber(a1 - a0), 88, "数组步长==88")

T.done()
