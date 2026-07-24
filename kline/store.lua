-- kline/store.lua —— 多 Series 容器:按 (symbol, period) 建/取/遍历
local Series = require("kline.series")

local Store = {}
Store.__index = Store

local function key(symbol, period) return symbol .. "|" .. period end

function Store.new(config)
  local self = setmetatable({}, Store)
  self.config = config or {}
  self._map = {}   -- "symbol|period" -> Series
  return self
end

-- 取或建一条序列
function Store:series(symbol, period)
  local k = key(symbol, period)
  local s = self._map[k]
  if not s then
    s = Series.new(symbol, period)
    self._map[k] = s
  end
  return s
end

-- 只取不建(不存在返回 nil)
function Store:get(symbol, period)
  return self._map[key(symbol, period)]
end

-- 序列条数
function Store:count()
  local n = 0
  for _ in pairs(self._map) do n = n + 1 end
  return n
end

-- 遍历所有序列:for key, series in store:each() do ... end
function Store:each()
  return pairs(self._map)
end

return Store
