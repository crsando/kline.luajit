-- kline/persist.lua -- 按 trading_day 切分、以 bar_time 为主键的 CSV upsert
local bar_mod = require("kline.bar")
local codec = require("kline.codec")

local M = {}
local temp_seq = 0

local function shell_quote(s)
  return "'" .. s:gsub("'", "'\\''") .. "'"
end

local function ensure_dir(dir)
  assert(type(dir) == "string" and dir ~= "", "persist dir must be a non-empty string")
  local ok = os.execute("mkdir -p -- " .. shell_quote(dir))
  assert(ok == true or ok == 0, "failed to create persist dir: " .. dir)
end

local function safe_component(value, name)
  assert(type(value) == "string" and value:match("^[%w_.%-]+$"),
    "invalid " .. name .. " for persist path: " .. tostring(value))
  return value
end

local function file_path(dir, symbol, period, trading_day)
  symbol = safe_component(symbol, "symbol")
  period = safe_component(period, "period")
  return string.format("%s/%s_%s_%d.csv", dir, symbol, period, trading_day)
end

local function bar_time_of(b)
  local t = tonumber(b.bar_time)
  assert(t and t > 0, "persist requires a positive bar_time")
  return t
end

local function collect_closed(series, opts)
  local groups = {}
  for i = 1, series.len do
    local b = series:at(i)
    if bar_mod.is_closed(b) then
      local td = tonumber(b.trading_day)
      assert(td and td > 0, "persist requires a positive trading_day")
      local entries = groups[td]
      if not entries then
        entries = {}
        groups[td] = entries
      end
      local t = bar_time_of(b)
      -- Series 中同一时间出现多次时，后出现的值覆盖前值。
      entries[t] = { time = t, line = codec.format_bar(b, opts) }
    end
  end
  return groups
end

-- 返回 records、文件是否存在、文件是否含重复或乱序记录。
local function read_existing(path, opts)
  local fh, open_err, open_code = io.open(path, "r")
  if not fh then
    if open_code == 2 then return {}, false, false end -- ENOENT
    error("failed to open persist file " .. path .. ": " .. tostring(open_err))
  end

  local records, seen = {}, {}
  local needs_rewrite = false
  local previous_time = nil
  local line_no = 0
  for line in fh:lines() do
    line_no = line_no + 1
    if not codec.is_skip_line(line) then
      local bt, err = codec.parse_line(line, opts)
      if not bt then
        fh:close()
        error(string.format("invalid CSV row %s:%d: %s", path, line_no, err or "parse failed"))
      end
      local t = bar_time_of(bt)
      if seen[t] or (previous_time and t < previous_time) then needs_rewrite = true end
      seen[t] = true
      previous_time = t
      records[t] = { time = t, line = codec.format_bar(bt, opts) }
    end
  end
  local ok, err = fh:close()
  assert(ok, "failed to close persist file " .. path .. ": " .. tostring(err))
  return records, true, needs_rewrite
end

local function write_atomic(path, series, records, opts)
  temp_seq = temp_seq + 1
  local tmp = string.format("%s.tmp.%d.%d", path, os.time(), temp_seq)
  local fh, open_err = io.open(tmp, "w")
  assert(fh, "failed to open temp persist file " .. tmp .. ": " .. tostring(open_err))

  local function abort(message)
    pcall(function() fh:close() end)
    os.remove(tmp)
    error(message, 0)
  end

  local meta = {
    symbol = series.symbol,
    period = series.period,
    tz = opts.tz_name or "Asia/Shanghai",
  }
  local ok, err = fh:write(codec.header(meta), "\n")
  if not ok then abort("failed to write persist header " .. tmp .. ": " .. tostring(err)) end
  for i = 1, #records do
    ok, err = fh:write(records[i].line, "\n")
    if not ok then abort("failed to write persist row " .. tmp .. ": " .. tostring(err)) end
  end
  ok, err = fh:flush()
  if not ok then abort("failed to flush persist file " .. tmp .. ": " .. tostring(err)) end
  ok, err = fh:close()
  if not ok then
    os.remove(tmp)
    error("failed to close persist file " .. tmp .. ": " .. tostring(err), 0)
  end

  ok, err = os.rename(tmp, path)
  if not ok then
    os.remove(tmp)
    error("failed to replace persist file " .. path .. ": " .. tostring(err), 0)
  end
end

-- 合并 series 中所有已封口 bar。文件内 bar_time 是唯一键：
-- 不存在则新增，内容变化则更新，完全相同则跳过。返回新增或更新的 bar 数。
function M.persist(series, dir, opts)
  opts = opts or {}
  ensure_dir(dir)

  local groups = collect_closed(series, opts)
  local trading_days = {}
  for td in pairs(groups) do trading_days[#trading_days + 1] = td end
  table.sort(trading_days)

  local changed_total = 0
  for _, td in ipairs(trading_days) do
    local path = file_path(dir, series.symbol, series.period, td)
    local merged, existed, needs_rewrite = read_existing(path, opts)
    local changed = 0

    for t, incoming in pairs(groups[td]) do
      local current = merged[t]
      if not current or current.line ~= incoming.line then changed = changed + 1 end
      merged[t] = incoming
    end

    local ordered = {}
    for _, record in pairs(merged) do ordered[#ordered + 1] = record end
    table.sort(ordered, function(a, b) return a.time < b.time end)

    if changed > 0 or needs_rewrite or not existed then
      write_atomic(path, series, ordered, opts)
    end
    changed_total = changed_total + changed
  end
  return changed_total
end

-- 读一个 CSV 文件 -> bar table 数组。数据行损坏时抛错，避免静默加载零值。
function M.read_file(path, opts)
  opts = opts or {}
  local bars = {}
  local fh, open_err, open_code = io.open(path, "r")
  if not fh then
    if open_code == 2 then return bars end -- ENOENT
    error("failed to open persist file " .. path .. ": " .. tostring(open_err))
  end
  local line_no = 0
  for line in fh:lines() do
    line_no = line_no + 1
    if not codec.is_skip_line(line) then
      local bt, err = codec.parse_line(line, opts)
      if not bt then
        fh:close()
        error(string.format("invalid CSV row %s:%d: %s", path, line_no, err or "parse failed"))
      end
      bars[#bars + 1] = bt
    end
  end
  local ok, err = fh:close()
  assert(ok, "failed to close persist file " .. path .. ": " .. tostring(err))
  return bars
end

-- 读文件并追加进 series；返回读入根数。
function M.load_into(series, path, opts)
  local bars = M.read_file(path, opts)
  for _, bt in ipairs(bars) do series:append(bt) end
  return #bars
end

-- 便捷：按 symbol/period/trading_day 构造路径并加载。
function M.load(series, dir, trading_day, opts)
  local path = file_path(dir, series.symbol, series.period, trading_day)
  return M.load_into(series, path, opts)
end

return M
