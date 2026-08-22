import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { analyzeFile } from "../src/engine.js";

test("analyzes simplified cumulative-volume ticks", async () => {
  const directory = await mkdtemp(join(tmpdir(), "market-profile-dashboard-"));
  const path = join(directory, "20260701.csv");
  await writeFile(path, [
    "update_date,symbol,last_price,volume",
    "20260701 09:29:00.000,IC2608,100.2,10",
    "20260701 09:30:00.000,IC2608,100.2,10",
    "20260701 09:30:30.000,IC2608,100.2,20",
    "20260701 09:31:00.000,IC2608,100.2,25",
    "20260701 13:00:00.000,IC2608,101.0,30",
    "20260701 13:00:30.000,IC2608,100.2,35",
    "20260701 15:00:00.000,IC2608,100.2,35",
    "",
  ].join("\n"), "utf8");
  try {
    const result = await analyzeFile(path, "20260701", "IC2608");
    assert.equal(result.bars.length, 3);
    assert.equal(result.bars[0].volume, 10);
    assert.equal(result.bars[0].open, 100.2);
    assert.equal(result.bars[0].close, 100.2);
    assert.ok(result.vwap.length > 0);
    assert.equal(result.profile.length, 1);
    assert.equal(result.zones.length, 3);
    assert.ok(result.zones.every((zone) => zone.role === "support"));
    assert.equal(result.quality.outsideSessionTicks, 2);
    assert.equal(result.quality.volumeResets, 0);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
