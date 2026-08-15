#!/usr/bin/env luajit

--[[
设计方案

本文件是一个只负责下载和归档新浪期货 1 分钟线的独立 CLI，不作为 Lua 模块导出。
它通过 request.luajit 请求新浪 getFewMinLine 接口，显式解析 JSONP 中的
d/o/h/l/c/v/p 字段，并兼容历史上可能出现的七元素数组响应。

运行模式只有两种：

1. 默认模式抓取数据后，同时更新 DATABASE.md 约定的 raw 和 canonical/v1 文件。
   raw 以新浪 datetime 为主键，canonical 以左对齐后的 bar_time 为主键；新数据覆盖
   同一主键的旧数据，最终按时间升序写回。
2. --dry-run 只抓取并把 raw 数据写到 stdout，支持 CSV 或 JSON；该路径不读取日历、
   不创建目录、不获取文件锁，也不写入任何归档文件。

新浪 1 分钟时间戳按 bar 结束时间解释，canonical.bar_time 固定减 1 分钟。
trading_day 默认仅跳过周末，与主项目当前 Calendar 行为一致；可通过
--calendar-file 注入 JSON 数组或逐行日期文件，以覆盖中国法定节假日。

归档写入面向 Linux/WSL：每个目标文件使用独立 flock，锁覆盖读取、合并、临时文件
写入和替换全过程。临时文件与目标文件位于同一目录，完整写入并 fsync 后通过 rename
原子替换。单文件读取者因此不需要加锁。

request session 由 CLI 创建和关闭；事件循环也只由 CLI 入口运行。HTTP 状态、JSONP
结构、字段类型、时间格式、重复行和已有文件元信息都会显式校验，避免静默生成不可用
的历史测试数据。

数据接口来源：AKShare futures_zh_minute_sina（MIT License）及新浪财经公开行情接口。
]]

local ffi = require("ffi")
local uv = require("luv")
local request = require("request")
local json = require("cjson.safe")

ffi.cdef([[
int flock(int fd, int operation);
]])

local API_URL =
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/=" ..
    "/InnerFuturesNewService.getFewMinLine"

local SOURCE_COLUMNS = {
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "hold",
}

local KLINE_COLUMNS = {
    "bar_time",
    "trading_day",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "open_interest",
    "settlement",
    "tick_count",
    "flags",
}

local SOURCE_NAME = "akshare-sina"
local SCHEMA_VERSION = "v1"
local CLOSED_FLAG = 0x01
local NIGHT_FLAG = 0x04
local LOCK_EX = 2
local LOCK_UN = 8
local DIRECTORY_MODE = 493 -- 0755
local FILE_MODE = 420 -- 0644
local MAX_RESPONSE_BYTES = 4 * 1024 * 1024
local MAX_ARCHIVE_BYTES = 64 * 1024 * 1024

local USAGE = [[
Usage:
  luajit examples/sina_future.lua --symbol LH2611 [options]
  luajit examples/sina_future.lua --symbol LH2611 --dry-run [--format csv|json]

Options:
  --symbol SYMBOL         Sina futures contract, for example LH2611 or RB0
  --dry-run               Fetch only; do not read or write the archive
  --format FORMAT         Dry-run output format: csv (default) or json
  --data-home PATH        Override KLINE_DATA_HOME/XDG data directory
  --calendar weekday      Trading-day inference mode (default: weekday)
  --calendar-file PATH    JSON array or newline-delimited trading dates
  --calendar-id NAME      Metadata value for --calendar-file (default: external)
  --timeout SECONDS       Total HTTP timeout in seconds (default: 30)
  -h, --help              Show this help
]]

local function trim(value)
    return tostring(value):match("^%s*(.-)%s*$")
end

local function starts_with(value, prefix)
    return value:sub(1, #prefix) == prefix
end

local function validate_symbol(value)
    if type(value) ~= "string" then
        return nil, "--symbol is required"
    end

    local symbol = trim(value):upper()
    if #symbol < 2 or #symbol > 16 or not symbol:match("^[A-Z][A-Z0-9]*$") then
        return nil, string.format(
            "invalid Sina futures symbol %q; expected a code such as LH2611 or RB0",
            value
        )
    end
    return symbol
end

local function validate_metadata_token(value, option_name)
    if type(value) ~= "string" or
        not value:match("^[A-Za-z0-9_.-]+$") then
        return nil, string.format(
            "%s must contain only letters, digits, dot, underscore, or hyphen",
            option_name
        )
    end
    return value
end

local VALUE_OPTIONS = {
    symbol = true,
    format = true,
    ["data-home"] = true,
    calendar = true,
    ["calendar-file"] = true,
    ["calendar-id"] = true,
    timeout = true,
}

local function parse_args(argv)
    local values = {}
    local seen = {}
    local index = 1

    while index <= #argv do
        local token = argv[index]
        if token == "-h" or token == "--help" then
            if #argv ~= 1 then
                return nil, "--help cannot be combined with other options"
            end
            return { help = true }
        elseif token == "--dry-run" then
            if seen["dry-run"] then
                return nil, "duplicate option: --dry-run"
            end
            seen["dry-run"] = true
            values.dry_run = true
        elseif starts_with(token, "--") then
            local name, value = token:match("^%-%-([^=]+)=(.*)$")
            if not name then
                name = token:sub(3)
            end
            if not VALUE_OPTIONS[name] then
                return nil, "unknown option: --" .. tostring(name)
            end
            if seen[name] then
                return nil, "duplicate option: --" .. name
            end
            seen[name] = true

            if value == nil then
                index = index + 1
                value = argv[index]
                if value == nil then
                    return nil, "missing value for --" .. name
                end
            end
            if value == "" then
                return nil, "empty value for --" .. name
            end
            values[name:gsub("%-", "_")] = value
        else
            return nil, "unexpected positional argument: " .. token
        end
        index = index + 1
    end

    local symbol, symbol_err = validate_symbol(values.symbol)
    if not symbol then
        return nil, symbol_err
    end
    values.symbol = symbol

    values.format = values.format or "csv"
    if values.format ~= "csv" and values.format ~= "json" then
        return nil, "--format must be csv or json"
    end
    if values.format ~= "csv" and not values.dry_run then
        return nil, "--format is only meaningful with --dry-run"
    end

    values.calendar = values.calendar or "weekday"
    if values.calendar ~= "weekday" then
        return nil, "--calendar currently supports only weekday"
    end

    values.timeout = tonumber(values.timeout or "30")
    if not values.timeout or values.timeout <= 0 or values.timeout > 300 then
        return nil, "--timeout must be a number greater than 0 and at most 300"
    end

    if values.calendar_id then
        local calendar_id, calendar_id_err =
            validate_metadata_token(values.calendar_id, "--calendar-id")
        if not calendar_id then
            return nil, calendar_id_err
        end
        values.calendar_id = calendar_id
    elseif values.calendar_file then
        values.calendar_id = "external"
    end

    if values.calendar_id and not values.calendar_file and not values.dry_run then
        return nil, "--calendar-id requires --calendar-file"
    end

    return values
end

local function normalize_absolute_path(path)
    if type(path) ~= "string" or path == "" then
        return nil, "path must not be empty"
    end
    if path:find("\0", 1, true) then
        return nil, "path must not contain NUL"
    end

    if path == "~" or starts_with(path, "~/") then
        local home = os.getenv("HOME")
        if not home or home == "" then
            return nil, "HOME is not set; cannot expand " .. path
        end
        path = home .. path:sub(2)
    elseif starts_with(path, "~") then
        return nil, "only ~/... home expansion is supported"
    end

    if path:sub(1, 1) ~= "/" then
        path = uv.cwd() .. "/" .. path
    end

    local parts = {}
    for part in path:gmatch("[^/]+") do
        if part == ".." then
            if #parts == 0 then
                return nil, "path escapes the filesystem root: " .. path
            end
            parts[#parts] = nil
        elseif part ~= "." and part ~= "" then
            parts[#parts + 1] = part
        end
    end

    local normalized = "/" .. table.concat(parts, "/")
    if normalized == "/" then
        return nil, "the filesystem root cannot be used as the data directory"
    end
    return normalized
end

local function default_data_home()
    local path = os.getenv("KLINE_DATA_HOME")
    if path and path ~= "" then
        return normalize_absolute_path(path)
    end

    path = os.getenv("XDG_DATA_HOME")
    if path and path ~= "" then
        return normalize_absolute_path(path .. "/kline")
    end

    local home = os.getenv("HOME")
    if not home or home == "" then
        return nil, "HOME is not set and no KLINE_DATA_HOME was provided"
    end
    return normalize_absolute_path(home .. "/.local/share/kline")
end

local function join_path(...)
    local values = { ... }
    local result = values[1]
    for index = 2, #values do
        result = result:gsub("/+$", "") .. "/" ..
            tostring(values[index]):gsub("^/+", "")
    end
    return result
end

local function dirname(path)
    local parent = path:match("^(.*)/[^/]+$")
    if not parent or parent == "" then
        return "/"
    end
    return parent
end

local function ensure_directory(path)
    local current = ""
    for part in path:gmatch("[^/]+") do
        current = current .. "/" .. part
        local stat, stat_err = uv.fs_stat(current)
        if stat then
            if stat.type ~= "directory" then
                return nil, current .. " exists but is not a directory"
            end
        else
            local created, mkdir_err = uv.fs_mkdir(current, DIRECTORY_MODE)
            if not created then
                local raced_stat = uv.fs_stat(current)
                if not raced_stat or raced_stat.type ~= "directory" then
                    return nil, string.format(
                        "failed to create directory %s: %s",
                        current,
                        tostring(mkdir_err or stat_err)
                    )
                end
            end
        end
    end
    return true
end

local function read_optional_file(path, size_limit)
    local stat, stat_err = uv.fs_stat(path)
    if not stat then
        if tostring(stat_err):find("ENOENT", 1, true) then
            return { exists = false }
        end
        return nil, "failed to stat " .. path .. ": " .. tostring(stat_err)
    end
    if stat.type ~= "file" then
        return nil, path .. " is not a regular file"
    end
    if stat.size > size_limit then
        return nil, string.format(
            "%s is too large (%d bytes; limit is %d)",
            path,
            stat.size,
            size_limit
        )
    end

    local fd, open_err = uv.fs_open(path, "r", 0)
    if not fd then
        return nil, "failed to open " .. path .. ": " .. tostring(open_err)
    end
    local data, read_err = uv.fs_read(fd, stat.size, 0)
    local closed, close_err = uv.fs_close(fd)
    if not data then
        return nil, "failed to read " .. path .. ": " .. tostring(read_err)
    end
    if not closed then
        return nil, "failed to close " .. path .. ": " .. tostring(close_err)
    end
    return { exists = true, data = data }
end

local function atomic_write(path, content)
    local ok, dir_err = ensure_directory(dirname(path))
    if not ok then
        return nil, dir_err
    end

    local fd, temp_path_or_err = uv.fs_mkstemp(path .. ".XXXXXX")
    if not fd then
        return nil, "failed to create temporary file for " .. path .. ": " ..
            tostring(temp_path_or_err)
    end
    local temp_path = temp_path_or_err
    local is_open = true

    local function cleanup(message)
        if is_open then
            uv.fs_close(fd)
            is_open = false
        end
        uv.fs_unlink(temp_path)
        return nil, message
    end

    local chmod_ok, chmod_err = uv.fs_fchmod(fd, FILE_MODE)
    if not chmod_ok then
        return cleanup("failed to chmod " .. temp_path .. ": " .. tostring(chmod_err))
    end

    local offset = 1
    while offset <= #content do
        local written, write_err = uv.fs_write(fd, content:sub(offset))
        if not written then
            return cleanup("failed to write " .. temp_path .. ": " .. tostring(write_err))
        end
        if written == 0 then
            return cleanup("failed to write " .. temp_path .. ": zero-byte write")
        end
        offset = offset + written
    end

    local sync_ok, sync_err = uv.fs_fsync(fd)
    if not sync_ok then
        return cleanup("failed to fsync " .. temp_path .. ": " .. tostring(sync_err))
    end

    local close_ok, close_err = uv.fs_close(fd)
    is_open = false
    if not close_ok then
        uv.fs_unlink(temp_path)
        return nil, "failed to close " .. temp_path .. ": " .. tostring(close_err)
    end

    local rename_ok, rename_err = uv.fs_rename(temp_path, path)
    if not rename_ok then
        uv.fs_unlink(temp_path)
        return nil, string.format(
            "failed to replace %s: %s",
            path,
            tostring(rename_err)
        )
    end
    return true
end

local function with_exclusive_lock(lock_path, operation)
    local ok, dir_err = ensure_directory(dirname(lock_path))
    if not ok then
        return nil, dir_err
    end

    local fd, open_err = uv.fs_open(lock_path, "a", FILE_MODE)
    if not fd then
        return nil, "failed to open lock " .. lock_path .. ": " .. tostring(open_err)
    end

    if ffi.C.flock(fd, LOCK_EX) ~= 0 then
        local errno = ffi.errno()
        uv.fs_close(fd)
        return nil, string.format("failed to lock %s: errno %d", lock_path, errno)
    end

    local call_ok, result, operation_err = xpcall(operation, debug.traceback)
    local unlock_errno
    if ffi.C.flock(fd, LOCK_UN) ~= 0 then
        unlock_errno = ffi.errno()
    end
    local close_ok, close_err = uv.fs_close(fd)

    if not call_ok then
        return nil, result
    end
    if not result then
        return nil, operation_err
    end
    if unlock_errno then
        return nil, string.format("failed to unlock %s: errno %d", lock_path, unlock_errno)
    end
    if not close_ok then
        return nil, "failed to close lock " .. lock_path .. ": " .. tostring(close_err)
    end
    return result
end

local function is_leap_year(year)
    return year % 4 == 0 and (year % 100 ~= 0 or year % 400 == 0)
end

local function days_in_month(year, month)
    local days = { 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 }
    if month == 2 and is_leap_year(year) then
        return 29
    end
    return days[month]
end

local function valid_date(year, month, day)
    if not year or not month or not day or year < 1900 or year > 9999 or
        month < 1 or month > 12 then
        return false
    end
    return day >= 1 and day <= days_in_month(year, month)
end

local function parse_datetime(value)
    if type(value) ~= "string" then
        return nil, "datetime is not a string"
    end
    local year, month, day, hour, minute, second = value:match(
        "^(%d%d%d%d)%-(%d%d)%-(%d%d) (%d%d):(%d%d):(%d%d)$"
    )
    year = tonumber(year)
    month = tonumber(month)
    day = tonumber(day)
    hour = tonumber(hour)
    minute = tonumber(minute)
    second = tonumber(second)
    if not valid_date(year, month, day) or not hour or hour > 23 or
        not minute or minute > 59 or not second or second > 59 then
        return nil, "invalid datetime: " .. value
    end
    return {
        year = year,
        month = month,
        day = day,
        hour = hour,
        minute = minute,
        second = second,
    }
end

local function format_datetime(parts)
    return string.format(
        "%04d-%02d-%02d %02d:%02d:%02d",
        parts.year,
        parts.month,
        parts.day,
        parts.hour,
        parts.minute,
        parts.second
    )
end

local function format_date(year, month, day)
    return string.format("%04d%02d%02d", year, month, day)
end

local function increment_date(year, month, day)
    day = day + 1
    if day > days_in_month(year, month) then
        day = 1
        month = month + 1
        if month > 12 then
            month = 1
            year = year + 1
        end
    end
    return year, month, day
end

local function decrement_date(year, month, day)
    day = day - 1
    if day < 1 then
        month = month - 1
        if month < 1 then
            month = 12
            year = year - 1
        end
        day = days_in_month(year, month)
    end
    return year, month, day
end

local function subtract_one_minute(parts)
    local result = {
        year = parts.year,
        month = parts.month,
        day = parts.day,
        hour = parts.hour,
        minute = parts.minute - 1,
        second = parts.second,
    }
    if result.minute < 0 then
        result.minute = 59
        result.hour = result.hour - 1
        if result.hour < 0 then
            result.hour = 23
            result.year, result.month, result.day =
                decrement_date(result.year, result.month, result.day)
        end
    end
    return result
end

local function weekday(year, month, day)
    if month < 3 then
        month = month + 12
        year = year - 1
    end
    local century_year = year % 100
    local century = math.floor(year / 100)
    local zeller = (
        day + math.floor(13 * (month + 1) / 5) + century_year +
        math.floor(century_year / 4) + math.floor(century / 4) + 5 * century
    ) % 7
    return (zeller + 6) % 7 -- 0=Sunday, 6=Saturday
end

local function next_weekday(year, month, day, include_current)
    if not include_current then
        year, month, day = increment_date(year, month, day)
    end
    while true do
        local value = weekday(year, month, day)
        if value ~= 0 and value ~= 6 then
            return year, month, day
        end
        year, month, day = increment_date(year, month, day)
    end
end

local function parse_trade_date(value)
    value = trim(value)
    local year, month, day
    if value:match("^%d%d%d%d%d%d%d%d$") then
        year = tonumber(value:sub(1, 4))
        month = tonumber(value:sub(5, 6))
        day = tonumber(value:sub(7, 8))
    else
        year, month, day = value:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)$")
        year = tonumber(year)
        month = tonumber(month)
        day = tonumber(day)
    end
    if not valid_date(year, month, day) then
        return nil, "invalid trading date: " .. value
    end
    return format_date(year, month, day)
end

local function load_calendar(path, calendar_id)
    local normalized_path, path_err = normalize_absolute_path(path)
    if not normalized_path then
        return nil, path_err
    end

    local file, file_err = read_optional_file(normalized_path, MAX_ARCHIVE_BYTES)
    if not file then
        return nil, file_err
    end
    if not file.exists then
        return nil, "calendar file does not exist: " .. normalized_path
    end

    local dates = {}
    local content = file.data
    if content:match("^%s*%[") then
        local decoded, decode_err = json.decode(content)
        if type(decoded) ~= "table" then
            return nil, "invalid calendar JSON: " .. tostring(decode_err)
        end
        for index, value in ipairs(decoded) do
            local date_value, date_err = parse_trade_date(value)
            if not date_value then
                return nil, string.format("calendar entry %d: %s", index, date_err)
            end
            dates[#dates + 1] = date_value
        end
    else
        local line_number = 0
        for line in (content .. "\n"):gmatch("(.-)\n") do
            line_number = line_number + 1
            line = line:gsub("\r$", "")
            line = trim(line)
            if line ~= "" and line:sub(1, 1) ~= "#" then
                local date_value, date_err = parse_trade_date(line)
                if not date_value then
                    return nil, string.format("calendar line %d: %s", line_number, date_err)
                end
                dates[#dates + 1] = date_value
            end
        end
    end

    if #dates == 0 then
        return nil, "calendar file contains no trading dates: " .. normalized_path
    end
    table.sort(dates)

    local unique = {}
    for _, value in ipairs(dates) do
        if unique[#unique] ~= value then
            unique[#unique + 1] = value
        end
    end
    return {
        id = calendar_id,
        dates = unique,
        path = normalized_path,
    }
end

local function calendar_lower_bound(dates, target, strictly_greater)
    local low = 1
    local high = #dates + 1
    while low < high do
        local middle = math.floor((low + high) / 2)
        local qualifies = dates[middle] and
            (dates[middle] > target or (not strictly_greater and dates[middle] == target))
        if qualifies then
            high = middle
        else
            low = middle + 1
        end
    end
    return dates[low]
end

local function resolve_trading_day(parts, calendar)
    local after_night_open = parts.hour >= 18
    if calendar.dates then
        local natural_day = format_date(parts.year, parts.month, parts.day)
        local resolved = calendar_lower_bound(
            calendar.dates,
            natural_day,
            after_night_open
        )
        if not resolved then
            return nil, string.format(
                "calendar %s does not cover bars after %s",
                calendar.id,
                natural_day
            )
        end
        return resolved
    end

    local year, month, day = next_weekday(
        parts.year,
        parts.month,
        parts.day,
        not after_night_open
    )
    return format_date(year, month, day)
end

local function finite_number(value, name, row_index)
    local number = tonumber(value)
    if not number or number ~= number or number == math.huge or number == -math.huge then
        return nil, string.format("row %d has invalid %s", row_index, name)
    end
    return number
end

local function parse_source_row(value, row_index)
    if type(value) ~= "table" then
        return nil, string.format("row %d is not an object or array", row_index)
    end

    local datetime = value.d or value.datetime or value[1]
    local fields = {
        datetime = datetime,
        open = value.o or value.open or value[2],
        high = value.h or value.high or value[3],
        low = value.l or value.low or value[4],
        close = value.c or value.close or value[5],
        volume = value.v or value.volume or value[6],
        hold = value.p or value.hold or value[7],
    }

    local datetime_parts, datetime_err = parse_datetime(fields.datetime)
    if not datetime_parts then
        return nil, string.format("row %d: %s", row_index, datetime_err)
    end
    if datetime_parts.second ~= 0 then
        return nil, string.format(
            "row %d has a non-minute-aligned datetime: %s",
            row_index,
            fields.datetime
        )
    end

    for _, name in ipairs({ "open", "high", "low", "close", "volume", "hold" }) do
        local number, number_err = finite_number(fields[name], name, row_index)
        if not number then
            return nil, number_err
        end
        fields[name] = number
    end

    if fields.open <= 0 or fields.high <= 0 or fields.low <= 0 or fields.close <= 0 then
        return nil, string.format("row %d contains a non-positive price", row_index)
    end
    if fields.volume < 0 or fields.hold < 0 then
        return nil, string.format("row %d contains negative volume or open interest", row_index)
    end
    if fields.high < math.max(fields.open, fields.low, fields.close) or
        fields.low > math.min(fields.open, fields.high, fields.close) then
        return nil, string.format("row %d has inconsistent OHLC values", row_index)
    end
    return fields
end

local function parse_jsonp(body)
    local start_index = body:find("=(", 1, true)
    if not start_index then
        return nil, "Sina response is not JSONP"
    end
    local end_index = body:find(");", start_index + 2, true)
    if not end_index then
        return nil, "Sina JSONP response is incomplete"
    end

    local decoded, decode_err = json.decode(body:sub(start_index + 2, end_index - 1))
    if type(decoded) ~= "table" then
        return nil, "invalid Sina JSON: " .. tostring(decode_err)
    end
    if #decoded == 0 then
        return nil, "Sina returned no minute bars"
    end

    local rows = {}
    local seen = {}
    for index, value in ipairs(decoded) do
        local row, row_err = parse_source_row(value, index)
        if not row then
            return nil, row_err
        end
        if seen[row.datetime] then
            return nil, "Sina response contains duplicate datetime: " .. row.datetime
        end
        seen[row.datetime] = true
        rows[#rows + 1] = row
    end
    table.sort(rows, function(left, right)
        return left.datetime < right.datetime
    end)
    return rows
end

local function fetch_minutes(session, options)
    local response, request_err = session:get(API_URL, {
        params = {
            { "symbol", options.symbol },
            { "type", "1" },
        },
        timeout = {
            connect = math.min(5, options.timeout),
            total = options.timeout,
        },
        max_body_bytes = MAX_RESPONSE_BYTES,
    }):await()

    if not response then
        return nil, "failed to fetch Sina minute bars: " .. tostring(request_err)
    end
    if response.status_code < 200 or response.status_code >= 300 then
        return nil, string.format(
            "Sina returned HTTP %d for %s",
            response.status_code,
            options.symbol
        )
    end
    return parse_jsonp(response.body)
end

local function normalize_rows(source_rows, calendar)
    local rows = {}
    local seen = {}
    for index, source in ipairs(source_rows) do
        local source_parts, datetime_err = parse_datetime(source.datetime)
        if not source_parts then
            return nil, string.format("row %d: %s", index, datetime_err)
        end
        local bar_parts = subtract_one_minute(source_parts)
        local trading_day, trading_day_err = resolve_trading_day(bar_parts, calendar)
        if not trading_day then
            return nil, trading_day_err
        end

        local is_night = bar_parts.hour >= 18 or bar_parts.hour < 8
        local bar_time = format_datetime(bar_parts)
        if seen[bar_time] then
            return nil, "normalization produced duplicate bar_time: " .. bar_time
        end
        seen[bar_time] = true
        rows[#rows + 1] = {
            bar_time = bar_time,
            trading_day = trading_day,
            open = source.open,
            high = source.high,
            low = source.low,
            close = source.close,
            volume = source.volume,
            turnover = 0,
            open_interest = source.hold,
            settlement = 0,
            tick_count = 0,
            flags = CLOSED_FLAG + (is_night and NIGHT_FLAG or 0),
        }
    end
    return rows
end

local function format_number(value)
    return string.format("%.15g", value)
end

local function encode_source_csv(rows)
    local lines = { table.concat(SOURCE_COLUMNS, ",") }
    for _, row in ipairs(rows) do
        lines[#lines + 1] = table.concat({
            row.datetime,
            format_number(row.open),
            format_number(row.high),
            format_number(row.low),
            format_number(row.close),
            format_number(row.volume),
            format_number(row.hold),
        }, ",")
    end
    return table.concat(lines, "\n") .. "\n"
end

local function encode_kline_csv(rows, metadata)
    local lines = {
        string.format(
            "# symbol=%s period=1m tz=Asia/Shanghai schema=%s " ..
            "source=akshare/sina source_time=end calendar=%s " ..
            "night_start=18:00 day_start=08:00",
            metadata.symbol,
            SCHEMA_VERSION,
            metadata.calendar
        ),
        table.concat(KLINE_COLUMNS, ","),
    }

    for _, row in ipairs(rows) do
        lines[#lines + 1] = table.concat({
            row.bar_time,
            row.trading_day,
            format_number(row.open),
            format_number(row.high),
            format_number(row.low),
            format_number(row.close),
            format_number(row.volume),
            format_number(row.turnover),
            format_number(row.open_interest),
            format_number(row.settlement),
            tostring(row.tick_count),
            tostring(row.flags),
        }, ",")
    end
    return table.concat(lines, "\n") .. "\n"
end

local function split_lines(content)
    local lines = {}
    for line in (content .. "\n"):gmatch("(.-)\n") do
        line = line:gsub("\r$", "")
        if line ~= "" then
            lines[#lines + 1] = line
        end
    end
    return lines
end

local function split_csv_line(line)
    if line:find('"', 1, true) then
        return nil, "quoted CSV fields are not supported by this fixed schema"
    end
    local fields = {}
    for field in (line .. ","):gmatch("(.-),") do
        fields[#fields + 1] = field
    end
    return fields
end

local function validate_header(line, expected)
    local fields, split_err = split_csv_line(line)
    if not fields then
        return nil, split_err
    end
    if #fields ~= #expected then
        return nil, "unexpected CSV column count"
    end
    for index, name in ipairs(expected) do
        if fields[index] ~= name then
            return nil, string.format(
                "unexpected CSV column %d: expected %s, got %s",
                index,
                name,
                tostring(fields[index])
            )
        end
    end
    return true
end

local function parse_metadata(line)
    if line:sub(1, 1) ~= "#" then
        return nil, "canonical CSV has no metadata header"
    end
    local metadata = {}
    for token in line:sub(2):gmatch("%S+") do
        local key, value = token:match("^([^=]+)=(.+)$")
        if key then
            metadata[key] = value
        end
    end
    return metadata
end

local function validate_metadata(actual, expected, path)
    for key, value in pairs(expected) do
        if actual[key] ~= value then
            return nil, string.format(
                "%s has %s=%q; expected %q; use a compatible archive directory",
                path,
                key,
                tostring(actual[key]),
                value
            )
        end
    end
    return true
end

local function parse_integer(value, name, context)
    local number = tonumber(value)
    if not number or number ~= math.floor(number) then
        return nil, context .. " has invalid " .. name
    end
    return number
end

local function read_raw_rows(path, expected_year)
    local file, file_err = read_optional_file(path, MAX_ARCHIVE_BYTES)
    if not file then
        return nil, file_err
    end
    if not file.exists then
        return {}
    end

    local lines = split_lines(file.data)
    if #lines == 0 then
        return nil, path .. " is empty"
    end
    local header_ok, header_err = validate_header(lines[1], SOURCE_COLUMNS)
    if not header_ok then
        return nil, path .. ": " .. header_err
    end

    local rows = {}
    local seen = {}
    for index = 2, #lines do
        local fields, split_err = split_csv_line(lines[index])
        if not fields or #fields ~= #SOURCE_COLUMNS then
            return nil, string.format(
                "%s line %d: %s",
                path,
                index,
                split_err or "unexpected column count"
            )
        end
        local row, row_err = parse_source_row(fields, index - 1)
        if not row then
            return nil, path .. " line " .. index .. ": " .. row_err
        end
        if row.datetime:sub(1, 4) ~= expected_year then
            return nil, path .. " contains a row outside year " .. expected_year
        end
        if seen[row.datetime] then
            return nil, path .. " contains duplicate datetime " .. row.datetime
        end
        seen[row.datetime] = true
        rows[#rows + 1] = row
    end
    return rows
end

local function read_kline_rows(path, expected_metadata, expected_trading_day)
    local file, file_err = read_optional_file(path, MAX_ARCHIVE_BYTES)
    if not file then
        return nil, file_err
    end
    if not file.exists then
        return {}
    end

    local lines = split_lines(file.data)
    if #lines < 2 then
        return nil, path .. " is missing metadata or CSV header"
    end
    local metadata, metadata_err = parse_metadata(lines[1])
    if not metadata then
        return nil, path .. ": " .. metadata_err
    end
    local metadata_ok, validate_err = validate_metadata(metadata, expected_metadata, path)
    if not metadata_ok then
        return nil, validate_err
    end
    local header_ok, header_err = validate_header(lines[2], KLINE_COLUMNS)
    if not header_ok then
        return nil, path .. ": " .. header_err
    end

    local rows = {}
    local seen = {}
    for index = 3, #lines do
        local fields, split_err = split_csv_line(lines[index])
        if not fields or #fields ~= #KLINE_COLUMNS then
            return nil, string.format(
                "%s line %d: %s",
                path,
                index,
                split_err or "unexpected column count"
            )
        end
        local _, datetime_err = parse_datetime(fields[1])
        if datetime_err then
            return nil, path .. " line " .. index .. ": " .. datetime_err
        end
        local trading_day, day_err = parse_trade_date(fields[2])
        if not trading_day then
            return nil, path .. " line " .. index .. ": " .. day_err
        end
        if trading_day ~= expected_trading_day then
            return nil, path .. " contains a row for trading day " .. trading_day
        end

        local row = {
            bar_time = fields[1],
            trading_day = trading_day,
        }
        for field_index, name in ipairs({
            "open",
            "high",
            "low",
            "close",
            "volume",
            "turnover",
            "open_interest",
            "settlement",
        }) do
            local number, number_err = finite_number(
                fields[field_index + 2],
                name,
                index - 2
            )
            if not number then
                return nil, path .. " line " .. index .. ": " .. number_err
            end
            row[name] = number
        end
        local tick_count, tick_err = parse_integer(fields[11], "tick_count", path)
        if not tick_count then
            return nil, tick_err
        end
        local flags, flags_err = parse_integer(fields[12], "flags", path)
        if not flags then
            return nil, flags_err
        end
        row.tick_count = tick_count
        row.flags = flags

        if seen[row.bar_time] then
            return nil, path .. " contains duplicate bar_time " .. row.bar_time
        end
        seen[row.bar_time] = true
        rows[#rows + 1] = row
    end
    return rows
end

local function merge_rows(existing, incoming, key_name)
    local by_key = {}
    for _, row in ipairs(existing) do
        by_key[row[key_name]] = row
    end
    for _, row in ipairs(incoming) do
        by_key[row[key_name]] = row
    end

    local keys = {}
    for key in pairs(by_key) do
        keys[#keys + 1] = key
    end
    table.sort(keys)

    local merged = {}
    for _, key in ipairs(keys) do
        merged[#merged + 1] = by_key[key]
    end
    return merged
end

local function group_rows(rows, key_function)
    local groups = {}
    for _, row in ipairs(rows) do
        local key = key_function(row)
        groups[key] = groups[key] or {}
        groups[key][#groups[key] + 1] = row
    end
    return groups
end

local function sorted_keys(values)
    local keys = {}
    for key in pairs(values) do
        keys[#keys + 1] = key
    end
    table.sort(keys)
    return keys
end

local function lock_path(data_home, parts)
    return join_path(data_home, "locks", table.concat(parts, "__") .. ".lock")
end

local function archive_rows(source_rows, canonical_rows, options, calendar)
    local raw_groups = group_rows(source_rows, function(row)
        return row.datetime:sub(1, 4)
    end)
    local canonical_groups = group_rows(canonical_rows, function(row)
        return row.trading_day
    end)
    local expected_metadata = {
        symbol = options.symbol,
        period = "1m",
        tz = "Asia/Shanghai",
        schema = SCHEMA_VERSION,
        source = "akshare/sina",
        source_time = "end",
        calendar = calendar.id,
        night_start = "18:00",
        day_start = "08:00",
    }

    local raw_targets = {}
    for _, year in ipairs(sorted_keys(raw_groups)) do
        raw_targets[#raw_targets + 1] = {
            year = year,
            rows = raw_groups[year],
            path = join_path(
                options.data_home,
                "raw",
                SOURCE_NAME,
                options.symbol,
                "1m",
                year,
                options.symbol .. "_1m_sina.csv"
            ),
        }
    end

    local canonical_targets = {}
    for _, trading_day in ipairs(sorted_keys(canonical_groups)) do
        canonical_targets[#canonical_targets + 1] = {
            trading_day = trading_day,
            rows = canonical_groups[trading_day],
            path = join_path(
                options.data_home,
                "canonical",
                SCHEMA_VERSION,
                SOURCE_NAME,
                options.symbol,
                "1m",
                trading_day:sub(1, 4),
                string.format("%s_1m_%s.csv", options.symbol, trading_day)
            ),
        }
    end

    -- Detect incompatible files before the first write. Files are validated again
    -- while holding their locks because this preflight is not a synchronization step.
    for _, target in ipairs(raw_targets) do
        local existing, existing_err = read_raw_rows(target.path, target.year)
        if not existing then
            return nil, existing_err
        end
    end
    for _, target in ipairs(canonical_targets) do
        local existing, existing_err = read_kline_rows(
            target.path,
            expected_metadata,
            target.trading_day
        )
        if not existing then
            return nil, existing_err
        end
    end

    local raw_paths = {}
    for _, target in ipairs(raw_targets) do
        local current_target = target
        local target_lock = lock_path(options.data_home, {
            "raw",
            SOURCE_NAME,
            options.symbol,
            "1m",
            current_target.year,
        })
        local write_ok, write_err = with_exclusive_lock(target_lock, function()
            local existing, existing_err = read_raw_rows(
                current_target.path,
                current_target.year
            )
            if not existing then
                return nil, existing_err
            end
            local merged = merge_rows(existing, current_target.rows, "datetime")
            return atomic_write(current_target.path, encode_source_csv(merged))
        end)
        if not write_ok then
            return nil, write_err
        end
        raw_paths[#raw_paths + 1] = current_target.path
    end

    local canonical_paths = {}
    for _, target in ipairs(canonical_targets) do
        local current_target = target
        local target_lock = lock_path(options.data_home, {
            "canonical-v1",
            SOURCE_NAME,
            options.symbol,
            "1m",
            current_target.trading_day,
        })
        local write_ok, write_err = with_exclusive_lock(target_lock, function()
            local existing, existing_err = read_kline_rows(
                current_target.path,
                expected_metadata,
                current_target.trading_day
            )
            if not existing then
                return nil, existing_err
            end
            local merged = merge_rows(existing, current_target.rows, "bar_time")
            local content = encode_kline_csv(merged, {
                symbol = options.symbol,
                calendar = calendar.id,
            })
            return atomic_write(current_target.path, content)
        end)
        if not write_ok then
            return nil, write_err
        end
        canonical_paths[#canonical_paths + 1] = current_target.path
    end

    return {
        raw_paths = raw_paths,
        canonical_paths = canonical_paths,
    }
end

local function write_dry_run(rows, output_format)
    if output_format == "csv" then
        io.stdout:write(encode_source_csv(rows))
        return true
    end

    local encoded, encode_err = json.encode(rows)
    if not encoded then
        return nil, "failed to encode dry-run JSON: " .. tostring(encode_err)
    end
    io.stdout:write(encoded, "\n")
    return true
end

local function print_archive_summary(options, rows, result)
    io.stdout:write("data home: ", options.data_home, "\n")
    io.stdout:write("source rows: ", tostring(#rows), "\n")
    io.stdout:write(
        "source range: ",
        rows[1].datetime,
        " -> ",
        rows[#rows].datetime,
        "\n"
    )
    io.stdout:write("raw files: ", tostring(#result.raw_paths), "\n")
    for _, path in ipairs(result.raw_paths) do
        io.stdout:write("  ", path, "\n")
    end
    io.stdout:write("canonical files: ", tostring(#result.canonical_paths), "\n")
    for _, path in ipairs(result.canonical_paths) do
        io.stdout:write("  ", path, "\n")
    end
end

local function run(session, options)
    local rows, fetch_err = fetch_minutes(session, options)
    if not rows then
        return nil, fetch_err
    end

    if options.dry_run then
        return write_dry_run(rows, options.format)
    end

    local data_home, data_home_err
    if options.data_home then
        data_home, data_home_err = normalize_absolute_path(options.data_home)
    else
        data_home, data_home_err = default_data_home()
    end
    if not data_home then
        return nil, data_home_err
    end
    options.data_home = data_home

    local calendar = { id = "weekday" }
    if options.calendar_file then
        calendar, data_home_err = load_calendar(options.calendar_file, options.calendar_id)
        if not calendar then
            return nil, data_home_err
        end
    end

    local canonical_rows, normalize_err = normalize_rows(rows, calendar)
    if not canonical_rows then
        return nil, normalize_err
    end
    local result, archive_err = archive_rows(
        rows,
        canonical_rows,
        options,
        calendar
    )
    if not result then
        return nil, archive_err
    end

    print_archive_summary(options, rows, result)
    return true
end

local options, argument_err = parse_args(arg or {})
if not options then
    io.stderr:write("sina_future: ", argument_err, "\n")
    io.stderr:write("Try 'luajit examples/sina_future.lua --help' for usage.\n")
    os.exit(2)
end
if options.help then
    io.stdout:write(USAGE)
    os.exit(0)
end

local runtime_err
local exit_code = 1
local session = request.session({ cookies = false })
local thread = coroutine.create(function()
    local protected_ok, protected_err = xpcall(function()
        local run_ok, run_err = run(session, options)
        if run_ok then
            exit_code = 0
        else
            runtime_err = run_err
        end
    end, debug.traceback)
    if not protected_ok then
        runtime_err = protected_err
    end
    session:close()
end)

local started, start_err = coroutine.resume(thread)
if not started then
    runtime_err = start_err
    session:close()
end
uv.run()

if runtime_err then
    io.stderr:write("sina_future: ", tostring(runtime_err), "\n")
end
os.exit(exit_code)
