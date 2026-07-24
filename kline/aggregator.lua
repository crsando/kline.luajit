-- kline/aggregator.lua —— Tick → 1 分钟 bar 合成状态机(时段感知)
-- 封口触发:①跨分钟(下一根 tick 驱动) ②flush()(外部在休盘/收盘边界主动调用)。
-- 关键避坑:
--   * volume/turnover 用差值累加(CTP 推的是当日累计值)
--   * 脏 tick 过滤(价格<=0 / 时间倒退 / 非交易时段)
--   * 休盘不补空 bar(无 tick 就没有 bar)
local util = require("kline.util")
local types = require("kline.types")

local Aggregator = {}
Aggregator.__index = Aggregator

local function floor_minute_ms(ms)
  ms = tonumber(ms)
  return math.floor(ms / 60000) * 60000
end

-- calendar: 一个 Calendar 实例;opts.on_bar_closed(bar_table) 封口回调
function Aggregator.new(symbol, calendar, opts)
  opts = opts or {}
  local self = setmetatable({}, Aggregator)
  self.symbol = symbol
  self.cal = calendar
  self.tz = opts.tz or util.TZ_OFFSET
  self.on_closed = opts.on_bar_closed
  self.cur = nil            -- 当前未封口 bar(table)
  self.cur_minute = nil     -- 当前 bar 起始分钟 ms
  self.last_vol = nil       -- 上一有效 tick 的当日累计成交量
  self.last_to = nil        -- 上一有效 tick 的当日累计成交额
  self.last_time = nil      -- 上一有效 tick 时间(检测时间倒退)
  return self
end

function Aggregator:_open_new(minute, tick, vol_delta, to_delta)
  local td = tick.trading_day or self.cal:trading_day(tick.time)
  local flags = 0
  if self.cal.night_start then
    local m = util.minute_of_day(tick.time, self.tz)
    if m >= self.cal.night_start then flags = util.set_flag(flags, types.FLAG.NIGHT) end
  end
  self.cur = {
    bar_time      = minute,
    trading_day   = td,
    open          = tick.price,
    high          = tick.price,
    low           = tick.price,
    close         = tick.price,
    volume        = vol_delta,
    turnover      = to_delta,
    open_interest = tick.open_interest or 0,
    settlement    = 0,
    tick_count    = 1,
    flags         = flags,
  }
  self.cur_minute = minute
end

function Aggregator:_close_current()
  local c = self.cur
  if not c then return nil end
  c.flags = util.set_flag(c.flags or 0, types.FLAG.CLOSED)
  self.cur = nil
  self.cur_minute = nil
  if self.on_closed then self.on_closed(c) end
  return c
end

-- 喂一个 tick。tick = { time=ms, price, volume(当日累计), turnover(当日累计),
--                       open_interest, trading_day(可选,优先用 CTP 的) }
-- 返回本次封口的 bar(table)或 nil。
function Aggregator:on_tick(tick)
  local t = tick.time
  -- 脏 tick 过滤
  if not tick.price or tick.price <= 0 then return nil end
  if self.last_time and tonumber(t) < tonumber(self.last_time) then return nil end
  if not self.cal:in_session(t) then return nil end

  local minute = floor_minute_ms(t)
  local closed = nil

  -- 跨分钟:封口旧 bar
  if self.cur and minute > self.cur_minute then
    closed = self:_close_current()
  end

  -- volume/turnover 差值累加(CTP 累计值;跨日归零时 delta 为负,取 0)
  local vol_delta, to_delta = 0, 0
  if self.last_vol then
    vol_delta = (tick.volume or 0) - self.last_vol
    if vol_delta < 0 then vol_delta = 0 end
    to_delta = (tick.turnover or 0) - self.last_to
    if to_delta < 0 then to_delta = 0 end
  end
  self.last_vol = tick.volume or 0
  self.last_to = tick.turnover or 0
  self.last_time = t

  if not self.cur then
    self:_open_new(minute, tick, vol_delta, to_delta)
  else
    local c = self.cur
    if tick.price > c.high then c.high = tick.price end
    if tick.price < c.low then c.low = tick.price end
    c.close = tick.price
    c.volume = c.volume + vol_delta
    c.turnover = c.turnover + to_delta
    c.open_interest = tick.open_interest or c.open_interest
    c.tick_count = c.tick_count + 1
  end

  return closed
end

-- 主动封口当前未完成 bar(收盘 / 换节时由外部调用)
function Aggregator:flush()
  return self:_close_current()
end

return Aggregator
