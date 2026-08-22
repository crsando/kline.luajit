import test from "node:test";
import assert from "node:assert/strict";
import { buildApp } from "../src/api.js";

test("health endpoint reports MVP version", async () => {
  const app = buildApp();
  try {
    const response = await app.inject({ method: "GET", url: "/api/health" });
    assert.equal(response.statusCode, 200);
    const body = response.json();
    assert.equal(body.ok, true);
    assert.equal(body.algorithmVersion, "mvp-1");
  } finally {
    await app.close();
  }
});
