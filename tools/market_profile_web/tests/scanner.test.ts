import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import type { AppConfig } from "../src/types.js";
import { ReviewStore } from "../src/reviews.js";
import { DataScanner } from "../src/scanner.js";

async function fixture(): Promise<{ root: string; config: AppConfig }> {
  const root = await mkdtemp(join(tmpdir(), "kline-scan-"));
  const day = join(root, "20260109");
  const profile = join(day, "day_market_profile");
  const visual = join(profile, "visual");
  await mkdir(join(day, "1min"), { recursive: true });
  await mkdir(visual, { recursive: true });
  await writeFile(join(day, "IF2603_20260109.csv"), "header\n");
  await writeFile(join(day, "1min", "IF2603_1m_20260109.csv"), "header\n");
  await writeFile(join(profile, "result.json"), JSON.stringify({ selected_contracts: 1, levels: 1, events: 1 }));
  await writeFile(join(visual, "index.html"), "<html></html>");
  await writeFile(join(visual, "manifest.json"), JSON.stringify({ instruments: [{
    instrument: "IF2603", root: "IF", exchange: "CFFEX", tick_size: 0.2,
    available_time: "2026-01-09 11:30:00", detail: "details/IF.html",
    image: "images/IF.png", stats: { levels: 1 },
  }] }));
  const header = "trading_day,root,instrument,available_time,node_types,center,lower,upper,source,raw_volume_share,prominence_share,width_ticks,scale_persistence,ltp_vwap_distance_ticks\n";
  await writeFile(join(profile, "levels.csv"), header + "20260109,IF,IF2603,2026-01-09 11:30:00,VPOC,4727,4726,4728,LTP_RAW,0.1,0.2,2,4,0\n");
  await writeFile(join(profile, "events.csv"), header.replace("\n", ",role,touch_time,touch_price,outcome,resolution_time,resolution_price,reaction_ticks,penetration_ticks,mfe_ticks,mae_ticks,minutes_to_resolution\n") + "20260109,IF,IF2603,2026-01-09 11:30:00,VPOC,4727,4726,4728,LTP_RAW,0.1,0.2,2,4,0,support,2026-01-09 13:00:00.000,4727,bounce,2026-01-09 13:01:00.000,4730,2,1,4,1,1\n");
  const config: AppConfig = { host: "127.0.0.1", port: 4317, dataRoot: root, repoRoot: root, dbPath: join(root, "reviews.sqlite"), python: "python3" };
  return { root, config };
}

test("scans day status, instruments and stable zones", async () => {
  const { root, config } = await fixture();
  const store = new ReviewStore(config.dbPath);
  try {
    const scanner = new DataScanner(config, store);
    const days = await scanner.listDays();
    assert.equal(days.length, 1);
    assert.deepEqual(days[0], {
      date: "20260109", rawTickFiles: 1, oneMinuteFiles: 1, profileReady: true,
      visualReady: true, selectedContracts: 1, levels: 1, events: 1, reviews: 0,
    });
    const instruments = await scanner.listInstruments("20260109");
    assert.equal(instruments[0]?.detailUrl, "/data/20260109/visual/details/IF.html");
    const levels = await scanner.levels("20260109", "IF2603");
    assert.equal(levels.length, 1);
    assert.equal(levels[0]?.autoOutcome, "bounce");
    assert.equal(levels[0]?.role, "support");
    assert.equal(levels[0]?.zoneId.length, 20);
  } finally {
    store.close();
    await rm(root, { recursive: true, force: true });
  }
});
