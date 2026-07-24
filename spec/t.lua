-- spec/t.lua —— 极简测试断言 helper,零依赖。
-- 用法:文件开头 local T = require("spec.t"); T.suite("名字"); ...; T.done()
-- 失败时 T.done() 会以非零退出码结束,方便 shell/CI 判断。
local T = { pass = 0, fail = 0, name = "" }

function T.suite(n)
  T.name = n
  io.write("=== ", n, " ===\n")
end

local function record(cond, msg)
  if cond then
    T.pass = T.pass + 1
  else
    T.fail = T.fail + 1
    io.write(string.format("  x FAIL: %s\n", msg or "assertion failed"))
  end
end

function T.ok(v, msg) record(v and true or false, msg or "ok") end

function T.eq(got, want, msg)
  record(got == want,
    string.format("%s (got=%s want=%s)", msg or "eq", tostring(got), tostring(want)))
end

function T.approx(got, want, eps, msg)
  eps = eps or 1e-9
  record(math.abs(got - want) <= eps,
    string.format("%s (got=%s want=%s)", msg or "approx", tostring(got), tostring(want)))
end

function T.done()
  io.write(string.format("--- %s: %d passed, %d failed ---\n\n", T.name, T.pass, T.fail))
  if T.fail > 0 then os.exit(1) end
end

return T
