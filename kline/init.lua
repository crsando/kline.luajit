-- kline/init.lua —— 库入口,聚合对外 API(require "kline" 即可)
local M = {}

M.types      = require("kline.types")
M.util       = require("kline.util")
M.bar        = require("kline.bar")
M.Series     = require("kline.series")
M.Store      = require("kline.store")
M.config     = require("kline.config")
M.Calendar   = require("kline.calendar")
M.Aggregator = require("kline.aggregator")
M.Rollup     = require("kline.period")
M.Pipeline   = require("kline.pipeline")
M.codec      = require("kline.codec")
M.persist    = require("kline.persist")

-- 创建一个全局 store
function M.new(config)
  return M.Store.new(config)
end

-- 便捷:为某合约创建一条 tick→多周期 的处理流水线
function M.pipeline(store, symbol, opts)
  opts = opts or {}
  local sessions = opts.sessions or M.config.sessions_of(symbol)
  local cal = opts.calendar or M.Calendar.new(sessions)
  return M.Pipeline.new(store, symbol, cal, opts)
end

return M
