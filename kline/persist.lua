-- kline/persist.lua —— 落地调度:按 trading_day 切分增量落地 + 加载回放
local codec = require("kline.codec")

local M = {}

local function ensure_dir(dir)
  os.execute('mkdir -p "' .. dir .. '" 2>/dev/null')
end

local function file_path(dir, symbol, period, trading_day)
  return string.format("%s/%s_%s_%d.csv", dir, symbol, period, trading_day)
end

local function exists(path)
  local f = io.open(path, "r")
  if f then f:close(); return true end
  return false
end

-- 增量落地:把 series 中 last_persist_index 之后的 bar,按 trading_day 分组
-- 追加写入 {symbol}_{period}_{trading_day}.csv(首次写入自动加元信息头)。
-- 返回本次写入的 bar 根数。
function M.persist(series, dir, opts)
  opts = opts or {}
  ensure_dir(dir)

  local groups, order = {}, {}
  for i = series.last_persist_index + 1, series.len do
    local b = series:at(i)
    local td = tonumber(b.trading_day)
    if not groups[td] then groups[td] = {}; order[#order + 1] = td end
    local g = groups[td]
    g[#g + 1] = codec.format_bar(b, opts)
  end

  local written = 0
  for _, td in ipairs(order) do
    local path = file_path(dir, series.symbol, series.period, td)
    local need_header = not exists(path)
    local fh = assert(io.open(path, "a"))
    if need_header then
      fh:write(codec.header{ symbol = series.symbol, period = series.period }, "\n")
    end
    for _, line in ipairs(groups[td]) do
      fh:write(line, "\n")
      written = written + 1
    end
    fh:close()
  end

  series.last_persist_index = series.len
  return written
end

-- 读一个 CSV 文件 -> bar table 数组(跳过注释/列名/空行)
function M.read_file(path, opts)
  local bars = {}
  local fh = io.open(path, "r")
  if not fh then return bars end
  for line in fh:lines() do
    if not codec.is_skip_line(line) then
      local bt = codec.parse_line(line, opts)
      if bt then bars[#bars + 1] = bt end
    end
  end
  fh:close()
  return bars
end

-- 读文件并追加进 series;返回读入根数
function M.load_into(series, path, opts)
  local bars = M.read_file(path, opts)
  for _, bt in ipairs(bars) do series:append(bt) end
  return #bars
end

-- 便捷:按 symbol/period/trading_day 构造路径并加载
function M.load(series, dir, trading_day, opts)
  local path = file_path(dir, series.symbol, series.period, trading_day)
  return M.load_into(series, path, opts)
end

return M
