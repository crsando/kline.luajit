import { createHash } from "node:crypto";
import { readdir, readFile, stat } from "node:fs/promises";
import { basename, join } from "node:path";
import { recordsFromCsv } from "./csv.js";
import { assertDate, dayPath } from "./paths.js";
import type { AppConfig, DaySummary, InstrumentSummary, ZoneRecord } from "./types.js";
import type { ReviewStore } from "./reviews.js";

async function exists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}

async function countFiles(path: string, pattern: RegExp): Promise<number> {
  try {
    const entries = await readdir(path, { withFileTypes: true });
    return entries.filter((entry) => entry.isFile() && pattern.test(entry.name)).length;
  } catch {
    return 0;
  }
}

async function readJson<T>(path: string): Promise<T | null> {
  try {
    return JSON.parse(await readFile(path, "utf8")) as T;
  } catch {
    return null;
  }
}

async function readRecords(path: string): Promise<Record<string, string>[]> {
  try {
    return recordsFromCsv(await readFile(path, "utf8"));
  } catch {
    return [];
  }
}

export function zoneId(row: Record<string, string>): string {
  const source = [
    row.trading_day, row.instrument, row.node_types, row.lower, row.upper, row.available_time,
  ].join("|");
  return createHash("sha256").update(source).digest("hex").slice(0, 20);
}

export class DataScanner {
  constructor(readonly config: AppConfig, readonly reviews: ReviewStore) {}

  async listDays(): Promise<DaySummary[]> {
    let entries;
    try {
      entries = await readdir(this.config.dataRoot, { withFileTypes: true });
    } catch {
      return [];
    }
    const dates = entries
      .filter((entry) => entry.isDirectory() && /^\d{8}$/.test(entry.name))
      .map((entry) => entry.name)
      .sort()
      .reverse();
    return Promise.all(dates.map((date) => this.getDay(date)));
  }

  async getDay(date: string): Promise<DaySummary> {
    assertDate(date);
    const root = dayPath(this.config.dataRoot, date);
    const profile = join(root, "day_market_profile");
    const visual = join(profile, "visual");
    const result = await readJson<{ selected_contracts?: number; levels?: number; events?: number }>(join(profile, "result.json"));
    return {
      date,
      rawTickFiles: await countFiles(root, /\.csv$/),
      oneMinuteFiles: await countFiles(join(root, "1min"), /_1m_\d{8}\.csv$/),
      profileReady: await exists(join(profile, "levels.csv")),
      visualReady: await exists(join(visual, "index.html")),
      selectedContracts: result?.selected_contracts ?? 0,
      levels: result?.levels ?? 0,
      events: result?.events ?? 0,
      reviews: this.reviews.countByDay(date),
    };
  }

  async listInstruments(date: string): Promise<InstrumentSummary[]> {
    assertDate(date);
    const visual = join(dayPath(this.config.dataRoot, date), "day_market_profile", "visual");
    const manifest = await readJson<{
      instruments?: Array<{
        instrument: string;
        root: string;
        exchange: string;
        tick_size: number;
        available_time: string;
        detail: string;
        image: string;
        stats: Record<string, number>;
      }>;
    }>(join(visual, "manifest.json"));
    return (manifest?.instruments ?? []).map((item) => ({
      instrument: item.instrument,
      root: item.root,
      exchange: item.exchange,
      tickSize: item.tick_size,
      availableTime: item.available_time,
      detailUrl: `/data/${date}/visual/${item.detail}`,
      imageUrl: `/data/${date}/visual/${item.image}`,
      stats: item.stats,
      reviews: this.reviews.countByInstrument(date, item.instrument),
    }));
  }

  async levels(date: string, instrument: string): Promise<ZoneRecord[]> {
    assertDate(date);
    if (!/^[A-Za-z0-9_\u4e00-\u9fff-]+$/.test(instrument)) throw new Error("Invalid instrument");
    const profile = join(dayPath(this.config.dataRoot, date), "day_market_profile");
    const levels = (await readRecords(join(profile, "levels.csv"))).filter((row) => row.instrument === instrument);
    const events = (await readRecords(join(profile, "events.csv"))).filter((row) => row.instrument === instrument);
    const eventMap = new Map(events.map((row) => [
      [row.node_types, row.center, row.lower, row.upper].join("|"), row,
    ]));
    return levels.map((row) => {
      const event = eventMap.get([row.node_types, row.center, row.lower, row.upper].join("|"));
      const id = zoneId(row);
      return {
        zoneId: id,
        tradingDay: row.trading_day ?? date,
        instrument: row.instrument ?? instrument,
        nodeType: row.node_types ?? "UNKNOWN",
        center: Number(row.center),
        lower: Number(row.lower),
        upper: Number(row.upper),
        availableTime: row.available_time ?? "",
        role: event?.role ?? "neutral",
        autoOutcome: event?.outcome ?? "no_touch",
        persistence: Number(row.scale_persistence),
        prominence: Number(row.prominence_share),
        review: this.reviews.get(date, instrument, id),
      };
    });
  }

  async sourceName(date: string, instrument: string): Promise<string | null> {
    const rows = await readRecords(join(dayPath(this.config.dataRoot, date), "day_market_profile", "selected_contracts.csv"));
    return rows.find((row) => row.instrument === instrument)?.source_file ?? null;
  }

  dayLabel(path: string): string {
    return basename(path);
  }
}
