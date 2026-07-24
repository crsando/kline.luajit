-- kline/series.lua —— 单条 (symbol, period) 时间序列的内存管理
-- 用预分配 + 倍增扩容的 FFI 数组存储连续 Bar,支持追加/随机访问/二分/切片。
-- 说明:M1 先用 ffi.new(GC 管理)。若未来数据量巨大要绕开 LuaJIT 2GB 堆限制,
--       可在此处切换为 ffi.C.malloc + ffi.gc 的 off-heap 分配,对外接口不变。
local ffi = require("ffi")
local types = require("kline.types")
local bar_mod = require("kline.bar")

local Series = {}
Series.__index = Series

local INIT_CAP = 256
local SIZEOF = types.SIZEOF_BAR

-- 创建序列。opts.cap 可指定初始容量。
function Series.new(symbol, period, opts)
  opts = opts or {}
  local self = setmetatable({}, Series)
  self.symbol = symbol
  self.period = period
  self.cap = opts.cap or INIT_CAP
  self.len = 0
  self.data = types.bar_array_t(self.cap)   -- kline_bar_t[cap]
  self.last_persist_index = 0               -- 增量落地水位线(1-based 已落地根数)
  return self
end

-- 倍增扩容,保证容量 >= need
function Series:_grow(need)
  if need <= self.cap then return end
  local newcap = self.cap
  while newcap < need do newcap = newcap * 2 end
  local newdata = types.bar_array_t(newcap)
  ffi.copy(newdata, self.data, self.len * SIZEOF)
  self.data = newdata
  self.cap = newcap
end

-- 追加一根 bar(b 可为 Lua table 或 cdata:kline_bar_t / 指针)。
-- 返回指向新元素的指针(可直接读写,供合成器更新未封口 bar)。
function Series:append(b)
  self:_grow(self.len + 1)
  local dstptr = self.data + self.len
  if type(b) == "table" then
    local nb = bar_mod.from_table(b)
    ffi.copy(dstptr, nb, SIZEOF)
  else -- cdata
    ffi.copy(dstptr, b, SIZEOF)
  end
  self.len = self.len + 1
  return dstptr
end

function Series:count() return self.len end

-- 第 i 根(1-based,支持负索引:-1=最新)。返回指针,越界返回 nil。
function Series:at(i)
  if i < 0 then i = self.len + i + 1 end
  if i < 1 or i > self.len then return nil end
  return self.data + (i - 1)
end

-- 最新一根(指针);空序列返回 nil
function Series:last()
  if self.len == 0 then return nil end
  return self.data + (self.len - 1)
end

-- lower_bound:返回第一个 bar_time >= t 的 0-based 位置(内部用)
function Series:_lower_bound(t)
  local data = self.data
  local lo, hi = 0, self.len
  while lo < hi do
    local mid = math.floor((lo + hi) / 2)
    if tonumber(data[mid].bar_time) < t then
      lo = mid + 1
    else
      hi = mid
    end
  end
  return lo
end

-- 精确定位 bar_time == t 的 1-based 索引;找不到返回 nil
function Series:find(t)
  local pos = self:_lower_bound(t)
  if pos < self.len and tonumber(self.data[pos].bar_time) == t then
    return pos + 1
  end
  return nil
end

-- 时间区间切片 [from_t, to_t)(左闭右开,与全局约定一致)。
-- 返回 start_index(1-based), count。视图语义,不拷贝数据。
function Series:slice(from_t, to_t)
  local s = self:_lower_bound(from_t)
  local e = self:_lower_bound(to_t)
  return s + 1, (e - s)
end

-- 遍历迭代器:for i, barptr in s:each() do ... end
function Series:each()
  local i = 0
  return function()
    i = i + 1
    if i > self.len then return nil end
    return i, self.data + (i - 1)
  end
end

return Series
