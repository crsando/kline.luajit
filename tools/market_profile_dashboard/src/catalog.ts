import { readdir, stat } from "node:fs/promises";
import { basename, join } from "node:path";
import { APP_CONFIG, contractMeta } from "./config.js";
import { assertDate, assertInstrument, resolveInside } from "./paths.js";
import type { CatalogEntry } from "./types.js";

const DATE_FILE_RE = /^(\d{8})\.csv$/;

export class Catalog {
  private readonly entries = new Map<string, CatalogEntry>();
  private loaded = false;

  async refresh(): Promise<void> {
    this.entries.clear();
    let instruments;
    try {
      instruments = await readdir(APP_CONFIG.dataRoot, { withFileTypes: true });
    } catch (error) {
      throw new Error(`Cannot read tick data root ${APP_CONFIG.dataRoot}: ${String(error)}`);
    }
    for (const instrumentDir of instruments) {
      if (!instrumentDir.isDirectory()) continue;
      const instrument = instrumentDir.name;
      let meta;
      try {
        meta = contractMeta(instrument);
      } catch {
        continue;
      }
      const dir = resolveInside(APP_CONFIG.dataRoot, instrument);
      for (const entry of await readdir(dir, { withFileTypes: true })) {
        if (!entry.isFile()) continue;
        const match = DATE_FILE_RE.exec(entry.name);
        if (!match) continue;
        const date = match[1];
        const path = resolveInside(dir, entry.name);
        const info = await stat(path);
        this.entries.set(this.key(date, instrument), {
          date,
          instrument,
          root: meta.root,
          tickSize: meta.tickSize,
          path,
          fileSize: info.size,
          modifiedAt: info.mtime.toISOString(),
        });
      }
    }
    this.loaded = true;
  }

  async ensureLoaded(): Promise<void> {
    if (!this.loaded) await this.refresh();
  }

  async listDays(): Promise<string[]> {
    await this.ensureLoaded();
    return [...new Set([...this.entries.values()].map((entry) => entry.date))].sort().reverse();
  }

  async listInstruments(date: string): Promise<CatalogEntry[]> {
    await this.ensureLoaded();
    assertDate(date);
    return [...this.entries.values()]
      .filter((entry) => entry.date === date)
      .sort((left, right) => left.instrument.localeCompare(right.instrument));
  }

  async get(date: string, instrument: string): Promise<CatalogEntry> {
    await this.ensureLoaded();
    assertDate(date);
    assertInstrument(instrument);
    const entry = this.entries.get(this.key(date, instrument));
    if (!entry) throw new Error(`No tick file for ${instrument} on ${date}`);
    const info = await stat(entry.path);
    if (info.size !== entry.fileSize || info.mtime.toISOString() !== entry.modifiedAt) {
      await this.refresh();
      const refreshed = this.entries.get(this.key(date, instrument));
      if (!refreshed) throw new Error(`No tick file for ${instrument} on ${date}`);
      return refreshed;
    }
    return entry;
  }

  private key(date: string, instrument: string): string {
    return `${date}:${basename(join("/", instrument))}`;
  }
}
