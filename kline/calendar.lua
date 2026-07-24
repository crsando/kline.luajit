-- kline/calendar.lua —— 交易时段判定 + 交易日历(trading_day 归属)
-- 只处理周末,不处理法定长假(与文华/vnpy 等主流软件一致;长假靠外部日历或 CTP 字段兜底)。
local util = require("kline.util")

local Calendar = {}
Calendar.__index = Calendar

-- 解析 "HH:MM-HH:MM,..." -> { {s=起始天内分钟, e=结束天内分钟}, ... }
local function parse_sessions(str)
  local segs = {}
  for part in str:gmatch("[^,]+") do
    local sh, sm, eh, em = part:match("(%d+):(%d+)%-(%d+):(%d+)")
    assert(sh, "bad session segment: " .. tostring(part))
    segs[#segs + 1] = { s = tonumber(sh) * 60 + tonumber(sm),
                        e = tonumber(eh) * 60 + tonumber(em) }
  end
  return segs
end

-- 天内分钟 m 是否落在段列表内(支持跨午夜段 s>e,如 23:00-02:30)
local function in_segs(segs, m)
  for i = 1, #segs do
    local s, e = segs[i].s, segs[i].e
    if s <= e then
      if m >= s and m < e then return true end
    else
      if m >= s or m < e then return true end
    end
  end
  return false
end

-- 从毫秒(UTC)求北京日期 YYYYMMDD
local function ymd_of(ms, tz)
  local sec = math.floor(tonumber(ms) / 1000) + tz
  local t = os.date("!*t", sec)
  return t.year * 10000 + t.month * 100 + t.day
end

-- 下一个交易日(跳过周六日;纯日期算术,本地时区自洽)
local function next_trading_day(yyyymmdd)
  local y  = math.floor(yyyymmdd / 10000)
  local mo = math.floor((yyyymmdd % 10000) / 100)
  local d  = yyyymmdd % 100
  local t = os.time({ year = y, month = mo, day = d, hour = 12, min = 0, sec = 0 })
  local wd
  repeat
    t = t + 86400
    wd = os.date("*t", t).wday      -- 1=Sun ... 7=Sat
  until wd ~= 1 and wd ~= 7
  local nt = os.date("*t", t)
  return nt.year * 10000 + nt.month * 100 + nt.day
end

function Calendar.new(sessions_str, opts)
  opts = opts or {}
  local self = setmetatable({}, Calendar)
  self.segs = parse_sessions(sessions_str)
  self.tz = opts.tz or util.TZ_OFFSET
  self.night_start = nil   -- 夜盘起点(天内分钟)
  self.day_close = nil     -- 日盘收盘(天内分钟,日盘段最大 e)
  for i = 1, #self.segs do
    local s, e = self.segs[i].s, self.segs[i].e
    if s >= 18 * 60 then
      self.night_start = self.night_start and math.min(self.night_start, s) or s
    end
    if s < 18 * 60 and s <= e then
      self.day_close = self.day_close and math.max(self.day_close, e) or e
    end
  end
  return self
end

function Calendar:has_night() return self.night_start ~= nil end

-- 是否在交易时段(过滤脏 tick 用)
function Calendar:in_session(ms)
  return in_segs(self.segs, util.minute_of_day(ms, self.tz))
end

-- 这一分钟是否是某连续交易段的最后一分钟(下一分钟离开交易时段)。
-- 用于 roll-up 的休盘/午休/收盘强制封口(核心避坑点)。
function Calendar:is_last_min_of_segment(ms)
  local m = util.minute_of_day(ms, self.tz)
  if not in_segs(self.segs, m) then return false end
  return not in_segs(self.segs, (m + 1) % 1440)
end

-- 是否命中交易日收盘(日盘最后一段结束前一分钟,如 14:59)。用于日线封口 / flush。
function Calendar:is_day_close(ms)
  if not self.day_close then return false end
  return util.minute_of_day(ms, self.tz) + 1 == self.day_close
end

-- 交易日 YYYYMMDD。优先用外部(CTP)传入;否则推算:夜盘归属下一交易日。
function Calendar:trading_day(ms, ctp_trading_day)
  if ctp_trading_day then return ctp_trading_day end
  local ymd = ymd_of(ms, self.tz)
  local m = util.minute_of_day(ms, self.tz)
  if self.night_start and m >= self.night_start then
    return next_trading_day(ymd)
  end
  return ymd
end

return Calendar
