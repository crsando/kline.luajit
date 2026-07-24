-- kline/util.lua —— 时间工具、位操作、日志(零外部依赖)
local bit = require("bit")

local M = {}

-- 交易所时区偏移(国内期货 = 北京时间 UTC+8),单位秒
M.TZ_OFFSET = 8 * 3600

--------------------------------------------------------------------------------
-- 时间转换:内部一律用 Unix 毫秒(UTC)存储,显示时按时区偏移格式化
--------------------------------------------------------------------------------

-- 毫秒(UTC) -> 可读字符串 "YYYY-MM-DD HH:MM:SS"(按 TZ_OFFSET,默认 UTC+8)
function M.ms_to_str(ms, tz_offset)
  tz_offset = tz_offset or M.TZ_OFFSET
  local sec = math.floor(tonumber(ms) / 1000) + tz_offset
  return os.date("!%Y-%m-%d %H:%M:%S", sec)
end

-- "YYYY-MM-DD HH:MM:SS"(按 TZ_OFFSET) -> 毫秒(UTC)
function M.str_to_ms(s, tz_offset)
  tz_offset = tz_offset or M.TZ_OFFSET
  local y, mo, d, h, mi, se = s:match("(%d+)%-(%d+)%-(%d+)%s+(%d+):(%d+):(%d+)")
  if not y then return nil end
  -- os.time 按本地时区解释,这里用 UTC 基准手动组装避免本机时区干扰
  local days_utc = os.time({ year=tonumber(y), month=tonumber(mo), day=tonumber(d),
                             hour=tonumber(h), min=tonumber(mi), sec=tonumber(se),
                             isdst=false })
  -- os.time 用了本地时区,补偿回 UTC:先拿本地偏移
  local local_off = os.difftime(os.time(os.date("*t")), os.time(os.date("!*t")))
  local utc_sec = days_utc + local_off - tz_offset
  return utc_sec * 1000LL
end

-- 当日分钟数 0..1439(按 TZ_OFFSET),用于分钟周期的时钟对齐
function M.minute_of_day(ms, tz_offset)
  tz_offset = tz_offset or M.TZ_OFFSET
  local sec = math.floor(tonumber(ms) / 1000) + tz_offset
  return math.floor((sec % 86400) / 60)
end

--------------------------------------------------------------------------------
-- 位标志操作(基于 LuaJIT bit 库)
--------------------------------------------------------------------------------
function M.has_flag(flags, mask) return bit.band(flags, mask) ~= 0 end
function M.set_flag(flags, mask) return bit.bor(flags, mask) end
function M.clear_flag(flags, mask) return bit.band(flags, bit.bnot(mask)) end

--------------------------------------------------------------------------------
-- 极简日志
--------------------------------------------------------------------------------
local LEVELS = { debug=1, info=2, warn=3, error=4 }
M.log_level = LEVELS.info

function M.log(level, ...)
  if (LEVELS[level] or 2) < M.log_level then return end
  io.write(string.format("[%s] ", level:upper()))
  print(...)
end

return M
