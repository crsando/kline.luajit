-- spec/persist_spec.lua —— 验证按 trading_day 切分的增量落地与加载
package.path = "./?.lua;./?/init.lua;" .. package.path
local T = require("spec.t")
local Series = require("kline.series")
local persist = require("kline.persist")
local util = require("kline.util")

T.suite("persist")

local DIR = "tmp_persist_test"
os.execute('rm -rf "' .. DIR .. '" 2>/dev/null')

local function ms(s) return util.str_to_ms(s) end

local s = Series.new("rb2510", "1m")
-- 两根属于交易日 20260724(日盘)
s:append{ bar_time = ms("2026-07-24 09:00:00"), trading_day = 20260724,
          open = 3200, high = 3205, low = 3199, close = 3202, volume = 100, open_interest = 210000 }
s:append{ bar_time = ms("2026-07-24 09:01:00"), trading_day = 20260724,
          open = 3202, high = 3210, low = 3201, close = 3208, volume = 80, open_interest = 210050 }
-- 一根夜盘,归属下一交易日 20260727
s:append{ bar_time = ms("2026-07-24 21:00:00"), trading_day = 20260727,
          open = 3208, high = 3212, low = 3206, close = 3210, volume = 60, open_interest = 210100 }

-- 首次落地:应写 3 根,生成 2 个文件(按 trading_day 切分)
local w1 = persist.persist(s, DIR, { price_fmt = "%.1f" })
T.eq(w1, 3, "首次落地写 3 根")

local f1 = io.open(DIR .. "/rb2510_1m_20260724.csv", "r")
T.ok(f1 ~= nil, "生成 20260724 文件")
if f1 then f1:close() end
local f2 = io.open(DIR .. "/rb2510_1m_20260727.csv", "r")
T.ok(f2 ~= nil, "生成 20260727 文件(夜盘归属)")
if f2 then f2:close() end

-- 增量:无新 bar,应写 0
local w2 = persist.persist(s, DIR, { price_fmt = "%.1f" })
T.eq(w2, 0, "无新增写 0(水位线生效)")

-- 追加新 bar 后增量落地
s:append{ bar_time = ms("2026-07-24 09:02:00"), trading_day = 20260724,
          open = 3208, high = 3215, low = 3207, close = 3212, volume = 90, open_interest = 210080 }
local w3 = persist.persist(s, DIR, { price_fmt = "%.1f" })
T.eq(w3, 1, "增量只写新增 1 根")

-- 读回 20260724 文件(应有 3 根)校验
local bars = persist.read_file(DIR .. "/rb2510_1m_20260724.csv", {})
T.eq(#bars, 3, "20260724 文件读回 3 根")
T.approx(bars[1].close, 3202, 1e-6, "读回第1根 close")
T.approx(bars[3].close, 3212, 1e-6, "读回第3根 close")
T.eq(bars[1].trading_day, 20260724, "读回 trading_day")

-- load_into 新序列,数据一致
local s2 = Series.new("rb2510", "1m")
local n = persist.load_into(s2, DIR .. "/rb2510_1m_20260724.csv", {})
T.eq(n, 3, "load_into 读入 3 根")
T.eq(s2:count(), 3, "新序列 3 根")
T.approx(s2:last().close, 3212, 1e-6, "新序列 last close 一致")
T.eq(tonumber(s2:at(1).bar_time), tonumber(ms("2026-07-24 09:00:00")), "bar_time 往返一致")

-- 清理
os.execute('rm -rf "' .. DIR .. '" 2>/dev/null')

T.done()
