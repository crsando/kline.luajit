-- kline/codec.lua —— CSV 编解码(可读优先,pandas/DuckDB 友好)
local util = require("kline.util")

local M = {}

-- 列顺序(与 pandas 读取列名一致)
M.COLUMNS = {
  "bar_time", "trading_day", "open", "high", "low", "close",
  "volume", "turnover", "open_interest", "settlement", "tick_count", "flags",
}

-- 元信息头:第一行 # 注释(pandas comment='#' 跳过)+ 第二行列名
function M.header(meta)
  meta = meta or {}
  local comment = string.format("# symbol=%s period=%s tz=%s generated_by=kline.lua",
    meta.symbol or "", meta.period or "", meta.tz or "Asia/Shanghai")
  return comment .. "\n" .. table.concat(M.COLUMNS, ",")
end

-- 格式化一根 bar(cdata)为 CSV 行。opts.price_fmt 默认 "%.2f"(按 tick size 定点)
function M.format_bar(b, opts)
  opts = opts or {}
  local pf = opts.price_fmt or "%.2f"
  local tz = opts.tz or util.TZ_OFFSET
  return table.concat({
    util.ms_to_str(b.bar_time, tz),            -- 可读时间字符串
    tostring(tonumber(b.trading_day)),
    string.format(pf, b.open),
    string.format(pf, b.high),
    string.format(pf, b.low),
    string.format(pf, b.close),
    string.format("%.0f", b.volume),
    string.format("%.2f", b.turnover),
    string.format("%.0f", b.open_interest),
    string.format(pf, b.settlement),
    tostring(tonumber(b.tick_count)),
    tostring(tonumber(b.flags)),
  }, ",")
end

-- 解析一行 CSV -> bar table(bar_time 解析回毫秒)。非法行返回 nil。
function M.parse_line(line, opts)
  opts = opts or {}
  local tz = opts.tz or util.TZ_OFFSET
  local f = {}
  for field in (line .. ","):gmatch("([^,]*),") do f[#f + 1] = field end
  if #f < 12 then return nil end
  return {
    bar_time      = util.str_to_ms(f[1], tz),
    trading_day   = tonumber(f[2]),
    open          = tonumber(f[3]),
    high          = tonumber(f[4]),
    low           = tonumber(f[5]),
    close         = tonumber(f[6]),
    volume        = tonumber(f[7]),
    turnover      = tonumber(f[8]),
    open_interest = tonumber(f[9]),
    settlement    = tonumber(f[10]),
    tick_count    = tonumber(f[11]),
    flags         = tonumber(f[12]),
  }
end

-- 判断是否为需跳过的行(注释头 / 列名行 / 空行)。返回纯布尔。
function M.is_skip_line(line)
  return line == "" or line:sub(1, 1) == "#" or line:match("^bar_time") ~= nil
end

return M
