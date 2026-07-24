-- spec/aggregator_spec.lua —— 验证 tick→1m 合成:同分钟聚合/跨分钟封口/
-- volume差值/脏tick过滤/休盘不补空/flush收盘封口
package.path = "./?.lua;./?/init.lua;" .. package.path
local T = require("spec.t")
local Aggregator = require("kline.aggregator")
local Calendar = require("kline.calendar")
local config = require("kline.config")
local util = require("kline.util")

T.suite("aggregator")

local cal = Calendar.new(config.sessions_of("rb2510"))
local closed = {}
local agg = Aggregator.new("rb2510", cal, {
  on_bar_closed = function(b) closed[#closed + 1] = b end,
})

local function ms(s) return util.str_to_ms(s) end
local function tk(ts, price, vol, oi)
  return agg:on_tick{ time = ms(ts), price = price, volume = vol, open_interest = oi }
end

-- 09:00 分钟内 3 笔(volume 为当日累计:10 -> 25 -> 40)
tk("2026-07-24 09:00:01", 3200, 10, 1000)
tk("2026-07-24 09:00:30", 3205, 25, 1010)
tk("2026-07-24 09:00:59", 3202, 40, 1005)
T.eq(#closed, 0, "同分钟未封口")

-- 跨分钟:09:01 tick 封口 09:00 bar
tk("2026-07-24 09:01:05", 3210, 50, 1020)
T.eq(#closed, 1, "跨分钟封口 1 根")
local b1 = closed[1]
T.eq(tonumber(b1.bar_time), tonumber(ms("2026-07-24 09:00:00")), "b1 bar_time 对齐 09:00:00")
T.approx(b1.open, 3200, 1e-6, "b1 open")
T.approx(b1.high, 3205, 1e-6, "b1 high")
T.approx(b1.low, 3200, 1e-6, "b1 low")
T.approx(b1.close, 3202, 1e-6, "b1 close")
T.approx(b1.volume, 30, 1e-6, "b1 volume 差值(15+15,首tick=0)")
T.eq(b1.tick_count, 3, "b1 tick_count=3")
T.eq(b1.trading_day, 20260724, "b1 trading_day")

-- 脏 tick:休盘 10:20 被忽略(且不污染 volume 基准)
tk("2026-07-24 10:20:00", 3300, 200, 1100)
T.eq(#closed, 1, "休盘脏 tick 不封口")

-- 跳到 10:14(跨分钟封口 09:01 bar);中间 09:02~10:13 无 tick 不补空
tk("2026-07-24 10:14:30", 3250, 100, 2000)
T.eq(#closed, 2, "跨到 10:14 封口 09:01")
local b2 = closed[2]
T.eq(tonumber(b2.bar_time), tonumber(ms("2026-07-24 09:01:00")), "b2 是 09:01(非空bar)")
T.approx(b2.volume, 10, 1e-6, "b2 volume=10(50-40,脏tick未污染)")

-- 10:14 同分钟第二笔
tk("2026-07-24 10:14:50", 3255, 120, 2010)

-- 跳到休盘后 10:30(封口 10:14 bar);验证中间 10:15~10:29 无空 bar
tk("2026-07-24 10:30:10", 3260, 150, 2020)
T.eq(#closed, 3, "休盘后封口 10:14,无空 bar")
local b3 = closed[3]
T.eq(tonumber(b3.bar_time), tonumber(ms("2026-07-24 10:14:00")), "b3 bar_time=10:14")
T.approx(b3.close, 3255, 1e-6, "b3 close=3255")
T.approx(b3.volume, 70, 1e-6, "b3 volume=70(50+20)")
T.eq(b3.tick_count, 2, "b3 tick_count=2")

-- flush 收盘封口 10:30 bar
local last = agg:flush()
T.ok(last ~= nil, "flush 返回最后一根")
T.eq(#closed, 4, "flush 后共 4 根")
T.eq(tonumber(closed[4].bar_time), tonumber(ms("2026-07-24 10:30:00")), "b4=10:30")
T.approx(closed[4].volume, 30, 1e-6, "b4 volume=30(150-120)")

-- 封口 bar 带 CLOSED 标志
T.ok(util.has_flag(b1.flags, 0x01), "封口 bar 有 CLOSED 标志")

-- 价格为 0 的脏 tick 被过滤
local before = #closed
agg:on_tick{ time = ms("2026-07-24 10:31:00"), price = 0, volume = 160 }
T.eq(#closed, before, "price<=0 脏 tick 忽略")

T.done()
