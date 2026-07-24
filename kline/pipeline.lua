-- kline/pipeline.lua —— 编织层:tick → 1m 合成 → 多周期 roll-up → 落到 store 各 series
-- 一个 Pipeline 管一个合约:on_tick 进来,自动合成 1m 并派生 5m/15m... 存入对应 series。
local Aggregator = require("kline.aggregator")
local Rollup     = require("kline.period")

local Pipeline = {}
Pipeline.__index = Pipeline

-- store: Store 实例;calendar: Calendar 实例;opts.periods: 派生周期分钟数(默认 {5,15})
function Pipeline.new(store, symbol, calendar, opts)
  opts = opts or {}
  local self = setmetatable({}, Pipeline)
  self.store  = store
  self.symbol = symbol
  self.cal    = calendar

  self.s1m = store:series(symbol, "1m")
  self.rollups = {}

  for _, p in ipairs(opts.periods or { 5, 15 }) do
    local rs = store:series(symbol, tostring(p) .. "m")
    local rollup = Rollup.new(p, calendar, {
      on_bar_closed = function(b) rs:append(b) end,
    })
    self.rollups[#self.rollups + 1] = rollup
  end

  -- 1m 合成器:封口时既存入 1m series,又喂给所有 rollup 派生更高周期
  self.agg = Aggregator.new(symbol, calendar, {
    on_bar_closed = function(b)
      self.s1m:append(b)
      for i = 1, #self.rollups do self.rollups[i]:update(b) end
    end,
  })
  return self
end

function Pipeline:on_tick(tick)
  return self.agg:on_tick(tick)
end

-- 收盘 / 换节:封口 1m(会一路带动 rollup),再封口各 rollup 的当前窗口
function Pipeline:flush()
  self.agg:flush()
  for i = 1, #self.rollups do self.rollups[i]:flush() end
end

return Pipeline
