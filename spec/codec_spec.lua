-- spec/codec_spec.lua —— 验证 CSV 编解码 round-trip
package.path = "./?.lua;./?/init.lua;" .. package.path
local T = require("spec.t")
local codec = require("kline.codec")
local bar_mod = require("kline.bar")
local util = require("kline.util")

T.suite("codec")

-- header
local h = codec.header{ symbol = "rb2510", period = "1m" }
T.ok(h:sub(1, 1) == "#", "header 首行是 # 注释")
T.ok(h:match("bar_time,trading_day,open"), "header 含列名行")
T.ok(h:match("symbol=rb2510"), "header 含 symbol 元信息")

-- format 一根 bar
local b = bar_mod.from_table{
  bar_time = util.str_to_ms("2026-07-24 09:30:00"),
  trading_day = 20260724, open = 3200, high = 3210, low = 3195,
  close = 3208, volume = 1520, turnover = 48792000, open_interest = 210345,
}
local line = codec.format_bar(b, { price_fmt = "%.1f" })
T.ok(line:match("^2026%-07%-24 09:30:00,"), "行首是可读时间")
T.ok(line:match(",20260724,"), "含 trading_day")
T.ok(line:match(",3200.0,"), "价格定点 %.1f")

-- parse 回来数值一致
local bt = codec.parse_line(line, { })
T.eq(bt.trading_day, 20260724, "parse trading_day")
T.approx(bt.open, 3200, 1e-6, "parse open")
T.approx(bt.close, 3208, 1e-6, "parse close")
T.approx(bt.open_interest, 210345, 1e-6, "parse open_interest")

-- bar_time round-trip(整秒精度):字符串->ms->字符串一致
T.eq(tonumber(bt.bar_time), tonumber(util.str_to_ms("2026-07-24 09:30:00")),
     "bar_time round-trip")

-- is_skip_line
T.eq(codec.is_skip_line("# comment"), true, "跳过注释")
T.eq(codec.is_skip_line("bar_time,trading_day"), true, "跳过列名行")
T.eq(codec.is_skip_line(""), true, "跳过空行")
T.eq(codec.is_skip_line("  "), true, "跳过纯空白行")
T.eq(codec.is_skip_line("2026-07-24 09:30:00,20260724,3200"), false, "数据行不跳")

-- 非法数据行必须拒绝,不能静默生成零值 bar
local invalid, parse_err = codec.parse_line("bad-time,x,x,x,x,x,x,x,x,x,x,x")
T.eq(invalid, nil, "非法 CSV 行返回 nil")
T.ok(type(parse_err) == "string", "非法 CSV 行返回错误原因")

T.done()
