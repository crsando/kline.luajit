import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { csvEscape } from "./csv.js";
import type { ReviewInput, ReviewRecord } from "./types.js";

interface ReviewRow {
  trading_day: string;
  instrument: string;
  zone_id: string;
  node_type: string;
  center: number;
  lower_price: number;
  upper_price: number;
  auto_outcome: string;
  manual_grade: string;
  manual_behavior: string;
  profile_quality: string;
  zone_quality: string;
  comment: string;
  created_at: string;
  updated_at: string;
}

function mapRow(row: ReviewRow): ReviewRecord {
  return {
    tradingDay: row.trading_day,
    instrument: row.instrument,
    zoneId: row.zone_id,
    nodeType: row.node_type,
    center: row.center,
    lower: row.lower_price,
    upper: row.upper_price,
    autoOutcome: row.auto_outcome,
    manualGrade: row.manual_grade as ReviewInput["manualGrade"],
    manualBehavior: row.manual_behavior as ReviewInput["manualBehavior"],
    profileQuality: row.profile_quality as ReviewInput["profileQuality"],
    zoneQuality: row.zone_quality as ReviewInput["zoneQuality"],
    comment: row.comment,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

export class ReviewStore {
  readonly db: DatabaseSync;

  constructor(path: string) {
    mkdirSync(dirname(path), { recursive: true });
    this.db = new DatabaseSync(path);
    this.db.exec("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;");
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS reviews (
        trading_day TEXT NOT NULL,
        instrument TEXT NOT NULL,
        zone_id TEXT NOT NULL,
        node_type TEXT NOT NULL,
        center REAL NOT NULL,
        lower_price REAL NOT NULL,
        upper_price REAL NOT NULL,
        auto_outcome TEXT NOT NULL,
        manual_grade TEXT NOT NULL DEFAULT '',
        manual_behavior TEXT NOT NULL DEFAULT '',
        profile_quality TEXT NOT NULL DEFAULT '',
        zone_quality TEXT NOT NULL DEFAULT '',
        comment TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (trading_day, instrument, zone_id)
      );
      CREATE INDEX IF NOT EXISTS idx_reviews_day ON reviews(trading_day);
    `);
  }

  close(): void {
    this.db.close();
  }

  countByDay(date: string): number {
    const row = this.db.prepare("SELECT COUNT(*) AS count FROM reviews WHERE trading_day = ?").get(date) as { count: number };
    return row.count;
  }

  countByInstrument(date: string, instrument: string): number {
    const row = this.db.prepare("SELECT COUNT(*) AS count FROM reviews WHERE trading_day = ? AND instrument = ?").get(date, instrument) as { count: number };
    return row.count;
  }

  get(date: string, instrument: string, zoneId: string): ReviewRecord | null {
    const row = this.db.prepare(
      "SELECT * FROM reviews WHERE trading_day = ? AND instrument = ? AND zone_id = ?",
    ).get(date, instrument, zoneId) as ReviewRow | undefined;
    return row ? mapRow(row) : null;
  }

  list(date: string, instrument?: string): ReviewRecord[] {
    const rows = instrument
      ? this.db.prepare("SELECT * FROM reviews WHERE trading_day = ? AND instrument = ? ORDER BY instrument, center").all(date, instrument)
      : this.db.prepare("SELECT * FROM reviews WHERE trading_day = ? ORDER BY instrument, center").all(date);
    return (rows as unknown as ReviewRow[]).map(mapRow);
  }

  upsert(
    zone: Pick<ReviewRecord, "tradingDay" | "instrument" | "zoneId" | "nodeType" | "center" | "lower" | "upper" | "autoOutcome">,
    input: ReviewInput,
  ): ReviewRecord {
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO reviews (
        trading_day, instrument, zone_id, node_type, center, lower_price, upper_price,
        auto_outcome, manual_grade, manual_behavior, profile_quality, zone_quality,
        comment, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(trading_day, instrument, zone_id) DO UPDATE SET
        node_type=excluded.node_type,
        center=excluded.center,
        lower_price=excluded.lower_price,
        upper_price=excluded.upper_price,
        auto_outcome=excluded.auto_outcome,
        manual_grade=excluded.manual_grade,
        manual_behavior=excluded.manual_behavior,
        profile_quality=excluded.profile_quality,
        zone_quality=excluded.zone_quality,
        comment=excluded.comment,
        updated_at=excluded.updated_at
    `).run(
      zone.tradingDay, zone.instrument, zone.zoneId, zone.nodeType, zone.center, zone.lower,
      zone.upper, zone.autoOutcome, input.manualGrade, input.manualBehavior,
      input.profileQuality, input.zoneQuality, input.comment, now, now,
    );
    return this.get(zone.tradingDay, zone.instrument, zone.zoneId)!;
  }

  delete(date: string, instrument: string, zoneId: string): boolean {
    const result = this.db.prepare(
      "DELETE FROM reviews WHERE trading_day = ? AND instrument = ? AND zone_id = ?",
    ).run(date, instrument, zoneId);
    return result.changes > 0;
  }

  exportCsv(date: string): string {
    const fields: (keyof ReviewRecord)[] = [
      "tradingDay", "instrument", "zoneId", "nodeType", "center", "lower", "upper",
      "autoOutcome", "manualGrade", "manualBehavior", "profileQuality", "zoneQuality",
      "comment", "createdAt", "updatedAt",
    ];
    const lines = [fields.join(",")];
    for (const record of this.list(date)) {
      lines.push(fields.map((field) => csvEscape(record[field])).join(","));
    }
    return lines.join("\n") + "\n";
  }
}
