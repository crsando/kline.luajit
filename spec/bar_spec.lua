-- spec/bar_spec.lua —— 验证 bar 构造 / 读写 / flags / 拷贝
package.path = "./?.lua;" .. package.path
local T = require("spec.t")
local bar = require("kline.bar")
local types = require("kline.types")

T.suite("bar")

local b = bar.from_table{
  bar_time      = 1700000000000LL,
  trading_day   = 20260724,
  open          = 3200,
  high          = 3210,
  low           = 3195,
  close         = 3208,
  volume        = 1520,
  turnover      = 48792000,
  open_interest = 210345,
}

-- 写入读回
T.eq(tonumber(b.trading_day), 20260724, "trading_day 写入")
T.approx(b.open, 3200, 1e-6, "open 写入")
T.approx(b.close, 3208, 1e-6, "close 写入")
T.approx(b.open_interest, 210345, 1e-6, "open_interest 写入")
T.eq(tonumber(b.bar_time), 1700000000000, "bar_time 写入(int64)")

-- flags
T.eq(bar.is_closed(b), false, "初始未封口")
bar.mark_closed(b)
T.eq(bar.is_closed(b), true, "mark_closed 后已封口")
bar.mark_night(b)
T.eq(bar.is_night(b), true, "mark_night 后夜盘")
T.eq(bar.is_closed(b), true, "夜盘标志不影响封口标志")

-- to_table 往返
local t = bar.to_table(b)
T.approx(t.high, 3210, 1e-6, "to_table high")
T.eq(t.trading_day, 20260724, "to_table trading_day")

-- 二进制拷贝
local b2 = types.bar_t()
bar.copy(b2, b)
T.approx(b2.open, 3200, 1e-6, "copy open")
T.approx(b2.volume, 1520, 1e-6, "copy volume")
T.eq(bar.is_closed(b2), true, "copy 保留 flags")

-- 缺失字段填 0
local b3 = bar.from_table{ close = 100 }
T.approx(b3.open, 0, 1e-9, "缺失字段填 0")
T.approx(b3.close, 100, 1e-6, "指定字段生效")

T.done()
