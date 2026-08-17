import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { ReviewStore } from "../src/reviews.js";

test("upserts reviews and exports escaped CSV", async () => {
  const root = await mkdtemp(join(tmpdir(), "kline-review-"));
  const store = new ReviewStore(join(root, "reviews.sqlite"));
  try {
    const zone = {
      tradingDay: "20260109", instrument: "IF2603", zoneId: "zone-1",
      nodeType: "HVN+VPOC", center: 4727, lower: 4724, upper: 4729,
      autoOutcome: "bounce",
    };
    store.upsert(zone, {
      manualGrade: "A", manualBehavior: "clean_bounce", profileQuality: "good",
      zoneQuality: "good", comment: "清晰, 快速反弹",
    });
    assert.equal(store.countByDay("20260109"), 1);
    assert.equal(store.countByInstrument("20260109", "IF2603"), 1);
    assert.equal(store.get("20260109", "IF2603", "zone-1")?.manualGrade, "A");
    store.upsert(zone, {
      manualGrade: "B", manualBehavior: "weak_bounce", profileQuality: "fair",
      zoneQuality: "good", comment: "updated",
    });
    assert.equal(store.list("20260109").length, 1);
    assert.equal(store.get("20260109", "IF2603", "zone-1")?.manualGrade, "B");
    const csv = store.exportCsv("20260109");
    assert.match(csv, /manualGrade/);
    assert.match(csv, /updated/);
    assert.equal(store.delete("20260109", "IF2603", "zone-1"), true);
    assert.equal(store.countByDay("20260109"), 0);
  } finally {
    store.close();
    await rm(root, { recursive: true, force: true });
  }
});
