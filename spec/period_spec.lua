-- spec/period_spec.lua —— 验证 1m→15m roll-up:时钟对齐/窗口切换/休盘强制封口
package.path = "./?.lua;./?/init.lua;" .. package.path
local T = require("spec.t")
local Rollup = require("kline.period")
local Calendar = require("kline.calendar")
local config = require("kline.config")
local util = require("kline.util")

T.suite("period")

local cal = Calendar.new(config.sessions_of("rb2510"))
local function ms(s) return util.str_to_ms(s) end
local function bar1m(hhmm, o, h, l, c, v)
  return { bar_time = ms("2026-07-24 " .. hhmm .. ":00"), trading_day = 20260724,
           open = o, high = h, low = l, close = c, volume = v, turnover = 0,
           open_interest = 1000, tick_count = 1 }
end

-------------------------------------------------------------------------------
-- 块A:基本 15m 聚合(09:00~09:14 一根,09:15 触发封口)
-------------------------------------------------------------------------------
local closedA = {}
local rA = Rollup.new(15, cal, { on_bar_closed = function(b) closedA[#closedA + 1] = b end })
for m = 0, 14 do
  rA:update(bar1m(string.format("09:%02d", m), 3200 + m, 3200 + m + 5, 3200 + m - 2, 3200 + m + 1, 10))
end
T.eq(#closedA, 0, "09:00~09:14 窗口未封口")
rA:update(bar1m("09:15", 3220, 3225, 3218, 3222, 10))   -- 新窗口触发封口
T.eq(#closedA, 1, "09:15 触发封口上一 15m")
local a = closedA[1]
T.eq(tonumber(a.bar_time), tonumber(ms("2026-07-24 09:00:00")), "bar_time=09:00 窗口起点")
T.approx(a.open, 3200, 1e-6, "open=首根 open")
T.approx(a.high, 3219, 1e-6, "high=max(3200+14+5)")
T.approx(a.low, 3198, 1e-6, "low=min(3200+0-2)")
T.approx(a.close, 3215, 1e-6, "close=09:14 close(3200+14+1)")
T.approx(a.volume, 150, 1e-6, "volume=15*10")
T.eq(a.tick_count, 15, "tick_count=15")

-------------------------------------------------------------------------------
-- 块B:休盘强制封口(10:00~10:14 满 15 根,10:14 是段最后一分钟自动封口)
-------------------------------------------------------------------------------
local closedB = {}
local rB = Rollup.new(15, cal, { on_bar_closed = function(b) closedB[#closedB + 1] = b end })
for m = 0, 14 do
  rB:update(bar1m(string.format("10:%02d", m), 3300, 3305, 3295, 3300, 8))
end
T.eq(#closedB, 1, "10:14 休盘边界强制封口")
T.eq(tonumber(closedB[1].bar_time), tonumber(ms("2026-07-24 10:00:00")), "封口 bar=10:00")
T.approx(closedB[1].volume, 120, 1e-6, "volume=15*8")

-- 休盘后 10:30 开新窗口(10:15~10:29 无 1m,不补空)
rB:update(bar1m("10:30", 3310, 3315, 3305, 3312, 8))
T.eq(#closedB, 1, "10:30 仅开新窗口,未产生空 bar")
rB:flush()
T.eq(#closedB, 2, "flush 封口 10:30 窗口")
T.eq(tonumber(closedB[2].bar_time), tonumber(ms("2026-07-24 10:30:00")), "第二根=10:30(干净起点)")

-------------------------------------------------------------------------------
-- 块C:5m 周期对齐(09:00~09:04 一根)
-------------------------------------------------------------------------------
local closedC = {}
local rC = Rollup.new(5, cal, { on_bar_closed = function(b) closedC[#closedC + 1] = b end })
for m = 0, 4 do rC:update(bar1m(string.format("09:%02d", m), 3200, 3210, 3190, 3205, 5)) end
rC:update(bar1m("09:05", 3205, 3206, 3204, 3205, 5))    -- 新 5m 窗口
T.eq(#closedC, 1, "5m: 09:00~09:04 封口")
T.approx(closedC[1].volume, 25, 1e-6, "5m volume=5*5")

T.done()
