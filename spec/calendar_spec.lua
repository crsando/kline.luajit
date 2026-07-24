-- spec/calendar_spec.lua —— 验证交易时段判定与 trading_day 归属
package.path = "./?.lua;./?/init.lua;" .. package.path
local T = require("spec.t")
local Calendar = require("kline.calendar")
local config = require("kline.config")
local util = require("kline.util")

T.suite("calendar")

local cal = Calendar.new(config.sessions_of("rb2510"))
local function ms(s) return util.str_to_ms(s) end

-- in_session(2026-07-24 周五)
T.eq(cal:in_session(ms("2026-07-24 09:30:00")), true,  "09:30 日盘中")
T.eq(cal:in_session(ms("2026-07-24 10:20:00")), false, "10:20 休盘")
T.eq(cal:in_session(ms("2026-07-24 12:00:00")), false, "12:00 午休")
T.eq(cal:in_session(ms("2026-07-24 22:00:00")), true,  "22:00 夜盘中")
T.eq(cal:in_session(ms("2026-07-24 16:00:00")), false, "16:00 非交易")
T.eq(cal:in_session(ms("2026-07-24 03:00:00")), false, "03:00 非交易")

-- is_last_min_of_segment(强制封口边界)
T.eq(cal:is_last_min_of_segment(ms("2026-07-24 10:14:00")), true,  "10:14 休盘前封口")
T.eq(cal:is_last_min_of_segment(ms("2026-07-24 11:29:00")), true,  "11:29 午休前封口")
T.eq(cal:is_last_min_of_segment(ms("2026-07-24 14:59:00")), true,  "14:59 收盘封口")
T.eq(cal:is_last_min_of_segment(ms("2026-07-24 22:59:00")), true,  "22:59 夜盘收封口")
T.eq(cal:is_last_min_of_segment(ms("2026-07-24 09:30:00")), false, "09:30 段中不封口")
T.eq(cal:is_last_min_of_segment(ms("2026-07-24 12:00:00")), false, "12:00 休盘中不封口")

-- is_day_close
T.eq(cal:is_day_close(ms("2026-07-24 14:59:00")), true,  "14:59 日盘收盘")
T.eq(cal:is_day_close(ms("2026-07-24 10:14:00")), false, "10:14 非日盘收盘")
T.eq(cal:is_day_close(ms("2026-07-24 22:59:00")), false, "22:59 非日盘收盘")

-- trading_day(2026-07-24 周五;25/26 周末;27 周一)
T.eq(cal:trading_day(ms("2026-07-24 10:00:00")), 20260724, "周五日盘=当天")
T.eq(cal:trading_day(ms("2026-07-24 22:00:00")), 20260727, "周五夜盘=下周一")
T.eq(cal:trading_day(ms("2026-07-22 22:00:00")), 20260723, "周三夜盘=周四")
T.eq(cal:trading_day(ms("2026-07-24 22:00:00"), 20260727), 20260727, "CTP override 优先")

-- 无夜盘品种(IF 股指)
local cal2 = Calendar.new(config.sessions_of("IF2506"))
T.eq(cal2:has_night(), false, "IF 无夜盘")
T.eq(cal2:in_session(ms("2026-07-24 09:45:00")), true,  "IF 09:45 交易")
T.eq(cal2:in_session(ms("2026-07-24 11:45:00")), false, "IF 11:45 午休")
T.eq(cal2:in_session(ms("2026-07-24 13:30:00")), true,  "IF 13:30 交易")
T.eq(cal2:is_day_close(ms("2026-07-24 14:59:00")), true, "IF 14:59 收盘")

T.done()
