-- kline/config.lua —— 品种交易时段表与配置
local M = {}

-- 品种(合约字母前缀)-> 交易时段字符串。
-- 格式:"HH:MM-HH:MM,..." 逗号分隔,左闭右开;夜盘段起点 >= 18:00 视为夜盘。
M.SESSIONS = {
  -- 上期所螺纹钢/热卷等(夜盘到 23:00)
  rb = "21:00-23:00,09:00-10:15,10:30-11:30,13:30-15:00",
  hc = "21:00-23:00,09:00-10:15,10:30-11:30,13:30-15:00",
  -- 无夜盘的商品(日盘 + 10:15 休盘 + 午休)
  DAY = "09:00-10:15,10:30-11:30,13:30-15:00",
  -- 中金所股指期货(无休盘、无夜盘)
  IF = "09:30-11:30,13:00-15:00",
  IC = "09:30-11:30,13:00-15:00",
  IH = "09:30-11:30,13:00-15:00",
}

-- 从合约代码提取字母前缀:rb2510 -> rb, IF2506 -> IF
function M.symbol_root(symbol)
  return (symbol:match("^(%a+)"))
end

-- 取某合约的交易时段字符串(找不到回退到日盘 DAY)
function M.sessions_of(symbol)
  local root = M.symbol_root(symbol) or ""
  return M.SESSIONS[root] or M.SESSIONS[root:upper()] or M.SESSIONS.DAY
end

return M
