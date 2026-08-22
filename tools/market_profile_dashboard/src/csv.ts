import { createReadStream } from "node:fs";
import { createInterface } from "node:readline";
import type { RawTick } from "./types.js";

function parseCsvLine(line: string): string[] {
  const values: string[] = [];
  let value = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (char === '"') {
      if (quoted && line[index + 1] === '"') {
        value += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (char === "," && !quoted) {
      values.push(value);
      value = "";
    } else {
      value += char;
    }
  }
  values.push(value);
  return values;
}

function parseEventTime(value: string): { normalized: string; timestamp: number; millisOfDay: number } {
  const match = /^(\d{4})(\d{2})(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?$/.exec(value.trim());
  if (!match) throw new Error(`Invalid update_date: ${value}`);
  const [, year, month, day, hour, minute, second, fraction = "0"] = match;
  const milliseconds = Number(fraction.padEnd(3, "0"));
  const timestamp = Date.UTC(
    Number(year), Number(month) - 1, Number(day),
    Number(hour), Number(minute), Number(second), milliseconds,
  ) - 8 * 60 * 60 * 1000;
  const normalized = `${year}-${month}-${day} ${hour}:${minute}:${second}.${String(milliseconds).padStart(3, "0")}`;
  const millisOfDay = ((Number(hour) * 60 + Number(minute)) * 60 + Number(second)) * 1000 + milliseconds;
  return { normalized, timestamp, millisOfDay };
}

export async function readRawTicks(path: string): Promise<RawTick[]> {
  const input = createInterface({
    input: createReadStream(path, { encoding: "utf8" }),
    crlfDelay: Infinity,
  });
  const ticks: RawTick[] = [];
  let header: string[] | null = null;
  let lineNumber = 0;
  for await (const rawLine of input) {
    lineNumber += 1;
    const line = String(rawLine).replace(/^\uFEFF/, "").trim();
    if (!line) continue;
    if (!header) {
      header = parseCsvLine(line);
      const required = ["update_date", "symbol", "last_price", "volume"];
      if (required.some((field) => !header!.includes(field))) {
        throw new Error(`${path}: unsupported CSV header`);
      }
      continue;
    }
    const values = parseCsvLine(line);
    const row = Object.fromEntries(header.map((key, index) => [key, values[index] ?? ""]));
    try {
      const event = parseEventTime(row.update_date);
      const lastPrice = Number(row.last_price);
      const cumulativeVolume = Number(row.volume);
      if (!Number.isFinite(lastPrice) || !Number.isFinite(cumulativeVolume)) throw new Error("non-finite number");
      ticks.push({
        eventTime: event.normalized,
        timestamp: event.timestamp,
        symbol: row.symbol,
        lastPrice,
        cumulativeVolume,
      });
    } catch (error) {
      throw new Error(`${path}:${lineNumber}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  if (!header) throw new Error(`${path}: empty CSV`);
  return ticks;
}
