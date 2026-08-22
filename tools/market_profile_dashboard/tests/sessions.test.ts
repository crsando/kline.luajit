import test from "node:test";
import assert from "node:assert/strict";
import { contractMeta } from "../src/config.js";
import { sessionFor } from "../src/sessions.js";

test("session boundaries are left-closed and right-open", () => {
  const meta = contractMeta("IC2608");
  assert.equal(sessionFor(9 * 60 * 60 * 1000 + 30 * 60 * 1000, meta), "morning");
  assert.equal(sessionFor(11 * 60 * 60 * 1000 + 30 * 60 * 1000, meta), null);
  assert.equal(sessionFor(13 * 60 * 60 * 1000, meta), "afternoon");
  assert.equal(sessionFor(15 * 60 * 60 * 1000, meta), null);
});
