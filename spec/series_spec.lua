-- spec/series_spec.lua —— 验证内存序列:追加/访问/二分/切片/扩容
package.path = "./?.lua;./?/init.lua;" .. package.path
local T = require("spec.t")
local Series = require("kline.series")
local types = require("kline.types")

T.suite("series")

-- 用小初始容量,方便测扩容
local s = Series.new("rb2510", "1m", { cap = 2 })
T.eq(s:count(), 0, "初始空")
T.eq(s:last(), nil, "空序列 last=nil")

-- 追加 5 根(bar_time 1000,2000,...5000),会触发多次扩容(cap 2->4->8)
for i = 1, 5 do
  s:append{ bar_time = i * 1000, open = 3200 + i, high = 3210 + i,
            low = 3190 + i, close = 3205 + i, volume = 10 * i,
            trading_day = 20260724 }
end
T.eq(s:count(), 5, "追加 5 根")
T.ok(s.cap >= 5, "容量已扩到 >=5 (got=" .. s.cap .. ")")

-- last / at
T.approx(s:last().close, 3210, 1e-6, "last close = 3205+5")
T.approx(s:at(1).open, 3201, 1e-6, "at(1) open")
T.approx(s:at(-1).close, 3210, 1e-6, "at(-1) = last")
T.approx(s:at(-2).close, 3209, 1e-6, "at(-2)")
T.eq(s:at(99), nil, "越界 at=nil")
T.eq(s:at(0), nil, "at(0)=nil")

-- 二分 find(精确匹配 bar_time)
T.eq(s:find(3000), 3, "find 3000 -> 第3根")
T.eq(s:find(1000), 1, "find 1000 -> 第1根")
T.eq(s:find(5000), 5, "find 5000 -> 第5根")
T.eq(s:find(3500), nil, "find 不存在 -> nil")

-- slice [2000, 4000) 应含 2000,3000 两根,起始索引=2
local si, cnt = s:slice(2000, 4000)
T.eq(si, 2, "slice 起始索引")
T.eq(cnt, 2, "slice 个数 [2000,4000)")

-- 扩容后数据完整性:逐根校验 close
local ok = true
for i, b in s:each() do
  if math.abs(b.close - (3205 + i)) > 1e-6 then ok = false end
end
T.ok(ok, "扩容后所有数据完整")

-- append 返回指针可直接改(供合成器更新未封口 bar)
local p = s:append{ bar_time = 6000, close = 999 }
p.close = 6666
T.approx(s:last().close, 6666, 1e-6, "append 返回指针可写")

-- 追加 cdata(struct)也支持
local nb = types.bar_t()
nb.bar_time = 7000; nb.close = 7777
s:append(nb)
T.approx(s:last().close, 7777, 1e-6, "append cdata")
T.eq(s:count(), 7, "总数 7")

T.done()
