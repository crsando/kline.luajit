-- kline/period.lua —— 1m → N分钟 bar 的 roll-up 聚合器
-- 对齐策略:时钟对齐(用天内分钟 // period 作窗口序号,国内时段起点整除,
--          每段第一根从时段起点干净开始)。
-- 休盘强制封口:某根 1m 是交易段最后一分钟(is_last_min_of_segment)时,封口目标 bar。
-- 缺失不补空:休盘期间没有 1m,自然不产生目标 bar。
local util = require("kline.util")
local types = require("kline.types")

local Rollup = {}
Rollup.__index = Rollup

-- period_min: 目标周期分钟数(5/15/30/60...);calendar 用于休盘封口判定
function Rollup.new(period_min, calendar, opts)
  opts = opts or {}
  local self = setmetatable({}, Rollup)
  self.p = period_min
  self.cal = calendar
  self.tz = opts.tz or util.TZ_OFFSET
  self.on_closed = opts.on_bar_closed
  self.cur = nil
  self.cur_key = nil    -- 当前目标窗口序号(天内分钟 // period)
  return self
end

function Rollup:_close()
  local c = self.cur
  if not c then return nil end
  c.flags = util.set_flag(c.flags or 0, types.FLAG.CLOSED)
  self.cur = nil
  self.cur_key = nil
  if self.on_closed then self.on_closed(c) end
  return c
end

-- 喂一根已封口的 1m bar(可为 table 或 cdata,字段访问方式相同)
-- 返回本次封口的目标 bar 或 nil
function Rollup:update(bar1m)
  local mod = util.minute_of_day(bar1m.bar_time, self.tz)
  local key = math.floor(mod / self.p)
  local closed = nil

  -- 窗口切换:封口旧目标 bar
  if self.cur and key ~= self.cur_key then
    closed = self:_close()
  end

  if not self.cur then
    self.cur = {
      bar_time      = tonumber(bar1m.bar_time),   -- 取窗口第一根 1m 的起始时间
      trading_day   = tonumber(bar1m.trading_day),
      open          = bar1m.open,
      high          = bar1m.high,
      low           = bar1m.low,
      close         = bar1m.close,
      volume        = bar1m.volume,
      turnover      = bar1m.turnover,
      open_interest = bar1m.open_interest,
      settlement    = 0,
      tick_count    = tonumber(bar1m.tick_count),
      flags         = 0,
    }
    self.cur_key = key
  else
    local c = self.cur
    if bar1m.high > c.high then c.high = bar1m.high end
    if bar1m.low  < c.low  then c.low  = bar1m.low end
    c.close = bar1m.close
    c.volume = c.volume + bar1m.volume
    c.turnover = c.turnover + bar1m.turnover
    c.open_interest = bar1m.open_interest      -- 持仓量取最新时点值
    c.tick_count = c.tick_count + tonumber(bar1m.tick_count)
  end

  -- 休盘/午休/收盘边界:这根 1m 是交易段最后一分钟 → 强制封口目标 bar
  if self.cal:is_last_min_of_segment(bar1m.bar_time) then
    closed = self:_close()
  end

  return closed
end

-- 主动封口(收盘 / 数据末尾)
function Rollup:flush() return self:_close() end

return Rollup
