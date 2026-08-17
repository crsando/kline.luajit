import test from "node:test";
import assert from "node:assert/strict";
import { join } from "node:path";
import { assertDate, resolveInside, visualPath } from "../src/paths.js";

test("validates real YYYYMMDD dates", () => {
  assert.equal(assertDate("20260109"), "20260109");
  assert.throws(() => assertDate("20260230"), /calendar/);
  assert.throws(() => assertDate("../20260109"), /YYYYMMDD/);
});

test("prevents paths escaping configured roots", () => {
  const root = "/tmp/safe-root";
  assert.equal(resolveInside(root, "20260109"), join(root, "20260109"));
  assert.throws(() => resolveInside(root, "../secret"), /escapes/);
  assert.throws(() => visualPath(root, "20260109", "../../secret.json"), /escapes/);
  assert.throws(() => visualPath(root, "20260109", "details/file.db"), /Unsupported/);
});
