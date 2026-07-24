-- kline/bar.lua —— 单根 Bar 的构造 / 读写 / 转换辅助
local ffi = require("ffi")
local types = require("kline.types")
local util = require("kline.util")

local M = {}
local bar_t = types.bar_t
local FLAG = types.FLAG

-- 从 Lua table 创建一个 bar(值类型 struct)。缺失字段填 0。
function M.from_table(t)
  local b = bar_t()
  b.bar_time      = t.bar_time or 0
  b.open          = t.open or 0
  b.high          = t.high or 0
  b.low           = t.low or 0
  b.close         = t.close or 0
  b.volume        = t.volume or 0
  b.turnover      = t.turnover or 0
  b.open_interest = t.open_interest or 0
  b.settlement    = t.settlement or 0
  b.trading_day   = t.trading_day or 0
  b.tick_count    = t.tick_count or 0
  b.flags         = t.flags or 0
  return b
end

-- bar(struct 或指针) -> Lua table(用于调试 / JSON 序列化)
function M.to_table(b)
  return {
    bar_time      = tonumber(b.bar_time),
    open          = b.open,
    high          = b.high,
    low           = b.low,
    close         = b.close,
    volume        = b.volume,
    turnover      = b.turnover,
    open_interest = b.open_interest,
    settlement    = b.settlement,
    trading_day   = b.trading_day,
    tick_count    = b.tick_count,
    flags         = b.flags,
  }
end

-- 二进制拷贝(dst/src 均为 kline_bar_t 或其指针)
function M.copy(dst, src)
  ffi.copy(dst, src, types.SIZEOF_BAR)
end

-- 可读单行摘要(调试用)
function M.tostring(b)
  return string.format("%s TD=%d O:%.2f H:%.2f L:%.2f C:%.2f V:%.0f OI:%.0f%s",
    util.ms_to_str(b.bar_time), tonumber(b.trading_day),
    b.open, b.high, b.low, b.close, b.volume, b.open_interest,
    M.is_closed(b) and " [closed]" or "")
end

--------------------------------------------------------------------------------
-- flags 位标志辅助
--------------------------------------------------------------------------------
function M.is_closed(b)  return util.has_flag(b.flags, FLAG.CLOSED) end
function M.mark_closed(b) b.flags = util.set_flag(b.flags, FLAG.CLOSED) end
function M.is_night(b)   return util.has_flag(b.flags, FLAG.NIGHT) end
function M.mark_night(b)  b.flags = util.set_flag(b.flags, FLAG.NIGHT) end

return M
