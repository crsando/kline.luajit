import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { buildApp } from "../src/app.js";

async function makeData(root: string): Promise<void> {
  const day = join(root, "20260109");
  const profile = join(day, "day_market_profile");
  const visual = join(profile, "visual");
  await mkdir(join(visual, "details"), { recursive: true });
  await writeFile(join(day, "IF2603.csv"), "header\n");
  await writeFile(join(profile, "result.json"), JSON.stringify({ selected_contracts: 1, levels: 1, events: 1 }));
  await writeFile(join(visual, "index.html"), "<html>visual</html>");
  await writeFile(join(visual, "manifest.json"), JSON.stringify({ instruments: [{ instrument: "IF2603", root: "IF", exchange: "CFFEX", tick_size: 0.2, available_time: "2026-01-09 11:30:00", detail: "details/IF.html", image: "images/IF.png", stats: { levels: 1 } }] }));
  await writeFile(join(visual, "details", "IF.html"), "<html>IF</html>");
  const header = "trading_day,root,instrument,available_time,node_types,center,lower,upper,source,raw_volume_share,prominence_share,width_ticks,scale_persistence,ltp_vwap_distance_ticks\n";
  await writeFile(join(profile, "levels.csv"), header + "20260109,IF,IF2603,2026-01-09 11:30:00,VPOC,4727,4726,4728,LTP_RAW,0.1,0.2,2,4,0\n");
  await writeFile(join(profile, "events.csv"), header.replace("\n", ",role,touch_time,touch_price,outcome,resolution_time,resolution_price,reaction_ticks,penetration_ticks,mfe_ticks,mae_ticks,minutes_to_resolution\n") + "20260109,IF,IF2603,2026-01-09 11:30:00,VPOC,4727,4726,4728,LTP_RAW,0.1,0.2,2,4,0,support,2026-01-09 13:00:00.000,4727,bounce,2026-01-09 13:01:00.000,4730,2,1,4,1,1\n");
}

test("serves day APIs, controlled visual files and review CRUD", async () => {
  const root = await mkdtemp(join(tmpdir(), "kline-api-"));
  const dataRoot = join(root, "data");
  await makeData(dataRoot);
  const repoRoot = resolve(process.cwd(), "../..");
  const app = buildApp({ dataRoot, repoRoot, dbPath: join(root, "reviews.sqlite"), host: "127.0.0.1", port: 4317, python: "python3" });
  try {
    let response = await app.inject({ method: "GET", url: "/api/health" });
    assert.equal(response.statusCode, 200);
    response = await app.inject({ method: "GET", url: "/api/days" });
    assert.equal(response.json()[0].date, "20260109");
    response = await app.inject({ method: "GET", url: "/api/days/20260109/instruments/IF2603/levels" });
    const zone = response.json()[0];
    assert.equal(zone.autoOutcome, "bounce");
    response = await app.inject({ method: "PUT", url: `/api/days/20260109/reviews/${zone.zoneId}`, payload: { instrument: "IF2603", manualGrade: "A", manualBehavior: "clean_bounce", profileQuality: "good", zoneQuality: "good", comment: "清晰" } });
    assert.equal(response.statusCode, 200);
    assert.equal(response.json().manualGrade, "A");
    response = await app.inject({ method: "GET", url: "/api/days/20260109/reviews.csv" });
    assert.match(response.body, /clean_bounce/);
    response = await app.inject({ method: "DELETE", url: `/api/days/20260109/reviews/${zone.zoneId}`, payload: { instrument: "IF2603" } });
    assert.deepEqual(response.json(), { deleted: true });
    response = await app.inject({ method: "GET", url: "/data/20260109/visual/index.html" });
    assert.equal(response.statusCode, 200);
    assert.match(response.body, /visual/);
    response = await app.inject({ method: "POST", url: "/api/days/20260109/run", payload: { step: "bad" } });
    assert.equal(response.statusCode, 400);
    response = await app.inject({ method: "GET", url: "/api/days/20260230" });
    assert.equal(response.statusCode, 400);
  } finally {
    await app.close();
    await rm(root, { recursive: true, force: true });
  }
});
