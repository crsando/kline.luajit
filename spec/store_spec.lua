-- spec/store_spec.lua —— 验证多 Series 容器
package.path = "./?.lua;./?/init.lua;" .. package.path
local T = require("spec.t")
local kline = require("kline")   -- 走 kline/init.lua

T.suite("store")

local store = kline.new()
T.eq(store:count(), 0, "初始无序列")

-- 取或建
local s1 = store:series("rb2510", "1m")
local s1b = store:series("rb2510", "1m")
T.ok(s1 == s1b, "同 key 返回同一 series")
T.eq(store:count(), 1, "一条序列")

local s2 = store:series("rb2510", "5m")
T.eq(store:count(), 2, "不同 period 独立序列")

local s3 = store:series("i2509", "1m")
T.eq(store:count(), 3, "不同 symbol 独立序列")

-- get 只取不建
T.ok(store:get("rb2510", "1m") == s1, "get 命中")
T.eq(store:get("cu2508", "1m"), nil, "get 未建返回 nil")
T.eq(store:count(), 3, "get 不新建")

-- 数据独立
s1:append{ bar_time = 1000, close = 100 }
s2:append{ bar_time = 1000, close = 200 }
T.approx(s1:last().close, 100, 1e-6, "s1 数据")
T.approx(s2:last().close, 200, 1e-6, "s2 数据独立")

-- 遍历
local n = 0
for k, ser in store:each() do n = n + 1 end
T.eq(n, 3, "each 遍历 3 条")

T.done()
