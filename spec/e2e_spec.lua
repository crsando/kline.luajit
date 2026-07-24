-- spec/e2e_spec.lua —— 端到端:tick → 1m 合成 → 5m/15m 派生 → 落地 → 加载回放
package.path = "./?.lua;./?/init.lua;" .. package.path
local T = require("spec.t")
local kline = require("kline")
local util = require("kline.util")

T.suite("e2e")

local DIR = "tmp_e2e_test"
os.execute('rm -rf "' .. DIR .. '" 2>/dev/null')

local store = kline.new()
local pipe = kline.pipeline(store, "rb2510", { periods = { 5, 15 } })

local function ms(s) return util.str_to_ms(s) end

-- 喂 09:00~09:16 每分钟一个 tick(:30 处),price=3200+分钟,volume 当日累计递增
for m = 0, 16 do
  local ts = string.format("2026-07-24 09:%02d:30", m)
  pipe:on_tick{ time = ms(ts), price = 3200 + m, volume = (m + 1) * 10, open_interest = 5000 + m }
end
pipe:flush()   -- 收盘封口最后未完成的 1m / 5m / 15m

local s1  = store:get("rb2510", "1m")
local s5  = store:get("rb2510", "5m")
local s15 = store:get("rb2510", "15m")

-- 根数:17 根 1m(09:00~09:16);5m 4 根;15m 2 根
T.eq(s1:count(), 17, "1m 共 17 根")
T.eq(s5:count(), 4,  "5m 共 4 根(00-04/05-09/10-14/15-16)")
T.eq(s15:count(), 2, "15m 共 2 根(00-14/15-16)")

-- 1m 首根(09:00):单 tick,open=close=3200
T.eq(tonumber(s1:at(1).bar_time), tonumber(ms("2026-07-24 09:00:00")), "1m#1 bar_time=09:00")
T.approx(s1:at(1).open, 3200, 1e-6, "1m#1 open=3200")

-- 5m 首根(09:00~09:04):open=3200, close=3204, high=3204, low=3200
local f5 = s5:at(1)
T.eq(tonumber(f5.bar_time), tonumber(ms("2026-07-24 09:00:00")), "5m#1 bar_time=09:00")
T.approx(f5.open, 3200, 1e-6, "5m#1 open=3200")
T.approx(f5.close, 3204, 1e-6, "5m#1 close=3204")
T.approx(f5.high, 3204, 1e-6, "5m#1 high=3204")
T.approx(f5.low, 3200, 1e-6, "5m#1 low=3200")
-- volume: 1m 09:00=0(首tick),09:01~09:04 各 delta10 → 5m#1 = 40
T.approx(f5.volume, 40, 1e-6, "5m#1 volume=40")

-- 15m 首根(09:00~09:14):open=3200 close=3214 high=3214
local f15 = s15:at(1)
T.approx(f15.open, 3200, 1e-6, "15m#1 open=3200")
T.approx(f15.close, 3214, 1e-6, "15m#1 close=3214")
T.approx(f15.high, 3214, 1e-6, "15m#1 high=3214")

-- 落地 1m 并读回校验(数据一致)
local w = kline.persist.persist(s1, DIR, { price_fmt = "%.1f" })
T.eq(w, 17, "落地 17 根 1m")

local s1b = kline.Series.new("rb2510", "1m")
local n = kline.persist.load(s1b, DIR, 20260724, {})
T.eq(n, 17, "从 CSV 读回 17 根")
T.approx(s1b:at(1).open, 3200, 1e-6, "读回 1m#1 open 一致")
T.approx(s1b:last().close, s1:last().close, 1e-6, "读回 last close 一致")
T.eq(tonumber(s1b:at(1).bar_time), tonumber(s1:at(1).bar_time), "读回 bar_time 一致")

os.execute('rm -rf "' .. DIR .. '" 2>/dev/null')

T.done()
