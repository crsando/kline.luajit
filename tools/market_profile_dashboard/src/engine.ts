import { contractMeta } from "./config.js";
import { readRawTicks } from "./csv.js";
import { sessionFor } from "./sessions.js";
import type {
  Bar,
  ChartPayload,
  NormalizedTick,
  ProfileBin,
  QualityStats,
  Zone,
} from "./types.js";

interface MutableBar {
  time: string;
  session: "morning" | "afternoon";
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  tickCount: number;
}

const nearlyEqual = (left: number, right: number): boolean => Math.abs(left - right) <= 1e-9;

function normalizeTicks(
  rawTicks: Awaited<ReturnType<typeof readRawTicks>>,
  tradingDay: string,
  instrument: string,
): { ticks: NormalizedTick[]; quality: QualityStats } {
  const meta = contractMeta(instrument);
  const quality: QualityStats = {
    rawTicks: rawTicks.length,
    validTicks: 0,
    outsideSessionTicks: 0,
    backwardsTicks: 0,
    invalidRows: 0,
    volumeResets: 0,
    zeroDeltaTicks: 0,
    morningTicks: 0,
    afternoonTicks: 0,
  };
  const ticks: NormalizedTick[] = [];
  let previousTimestamp: number | null = null;
  let previousVolume: number | null = null;
  for (const raw of rawTicks) {
    if (raw.symbol !== instrument || raw.lastPrice <= 0 || raw.cumulativeVolume < 0) {
      quality.invalidRows += 1;
      continue;
    }
    const millisOfDay = (((Number(raw.eventTime.slice(11, 13)) * 60
      + Number(raw.eventTime.slice(14, 16))) * 60
      + Number(raw.eventTime.slice(17, 19))) * 1000)
      + Number(raw.eventTime.slice(20, 23));
    const session = sessionFor(millisOfDay, meta);
    if (!session) {
      quality.outsideSessionTicks += 1;
      continue;
    }
    if (previousTimestamp !== null && raw.timestamp < previousTimestamp) {
      quality.backwardsTicks += 1;
      continue;
    }
    let deltaVolume = 0;
    const qualityFlags: string[] = [];
    if (previousVolume !== null) {
      deltaVolume = raw.cumulativeVolume - previousVolume;
      if (deltaVolume < 0) {
        deltaVolume = 0;
        quality.volumeResets += 1;
        qualityFlags.push("VOLUME_RESET");
      }
    }
    if (deltaVolume === 0) {
      quality.zeroDeltaTicks += 1;
      qualityFlags.push("ZERO_DELTA_VOLUME");
    }
    const barKey = raw.eventTime.slice(0, 16);
    ticks.push({
      ...raw,
      tradingDay,
      millisOfDay,
      session,
      deltaVolume,
      qualityFlags,
      barKey,
    });
    quality.validTicks += 1;
    if (session === "morning") quality.morningTicks += 1;
    else quality.afternoonTicks += 1;
    previousTimestamp = raw.timestamp;
    previousVolume = raw.cumulativeVolume;
  }
  return { ticks, quality };
}

function buildBars(ticks: NormalizedTick[]): { bars: Bar[]; indices: Map<string, number> } {
  const mutable = new Map<string, MutableBar>();
  for (const tick of ticks) {
    const current = mutable.get(tick.barKey);
    if (!current) {
      mutable.set(tick.barKey, {
        time: `${tick.barKey}:00`,
        session: tick.session,
        open: tick.lastPrice,
        high: tick.lastPrice,
        low: tick.lastPrice,
        close: tick.lastPrice,
        volume: tick.deltaVolume,
        tickCount: 1,
      });
      continue;
    }
    current.high = Math.max(current.high, tick.lastPrice);
    current.low = Math.min(current.low, tick.lastPrice);
    current.close = tick.lastPrice;
    current.volume += tick.deltaVolume;
    current.tickCount += 1;
  }
  const bars: Bar[] = [];
  const indices = new Map<string, number>();
  for (const [time, current] of mutable) {
    const index = bars.length;
    indices.set(time.slice(0, 16), index);
    bars.push({ index, ...current });
  }
  return { bars, indices };
}

function buildProfile(ticks: NormalizedTick[], tickSize: number): {
  bins: ProfileBin[];
  zones: Array<{ type: "VPOC" | "VAL" | "VAH"; center: number; lower: number; upper: number }>;
} {
  const volumes = new Map<number, number>();
  for (const tick of ticks) {
    if (tick.session !== "morning" || tick.deltaVolume <= 0) continue;
    const bin = Math.round(tick.lastPrice / tickSize);
    volumes.set(bin, (volumes.get(bin) ?? 0) + tick.deltaVolume);
  }
  if (volumes.size === 0) return { bins: [], zones: [] };
  const minBin = Math.min(...volumes.keys());
  const maxBin = Math.max(...volumes.keys());
  const values = Array.from({ length: maxBin - minBin + 1 }, (_, offset) => volumes.get(minBin + offset) ?? 0);
  const total = values.reduce((sum, value) => sum + value, 0);
  const bins = values.map((volume, offset) => ({
    price: (minBin + offset) * tickSize,
    volume,
    volumeShare: total > 0 ? volume / total : 0,
  }));
  const maximum = Math.max(...values);
  let poc = values.findIndex((value) => nearlyEqual(value, maximum));
  let pocLow = poc;
  let pocHigh = poc;
  while (pocLow > 0 && nearlyEqual(values[pocLow - 1], maximum)) pocLow -= 1;
  while (pocHigh + 1 < values.length && nearlyEqual(values[pocHigh + 1], maximum)) pocHigh += 1;
  poc = Math.floor((pocLow + pocHigh) / 2);
  let left = pocLow;
  let right = pocHigh;
  let accumulated = values.slice(left, right + 1).reduce((sum, value) => sum + value, 0);
  const target = total * 0.7;
  while (accumulated < target && (left > 0 || right + 1 < values.length)) {
    const leftValue = left > 0 ? values[left - 1] : -1;
    const rightValue = right + 1 < values.length ? values[right + 1] : -1;
    if (leftValue >= rightValue) {
      left -= 1;
      accumulated += values[left];
    } else {
      right += 1;
      accumulated += values[right];
    }
  }
  const priceAt = (index: number): number => (minBin + index) * tickSize;
  const halfTick = tickSize / 2;
  const zones = [
    {
      type: "VPOC" as const,
      center: (priceAt(pocLow) + priceAt(pocHigh)) / 2,
      lower: priceAt(pocLow) - halfTick,
      upper: priceAt(pocHigh) + halfTick,
    },
    { type: "VAL" as const, center: priceAt(left), lower: priceAt(left) - halfTick, upper: priceAt(left) + halfTick },
    { type: "VAH" as const, center: priceAt(right), lower: priceAt(right) - halfTick, upper: priceAt(right) + halfTick },
  ];
  return { bins, zones };
}

function addTouchRoles(
  zones: Array<{ type: "VPOC" | "VAL" | "VAH"; center: number; lower: number; upper: number }>,
  ticks: NormalizedTick[],
  indices: Map<string, number>,
  bars: Bar[],
): Zone[] {
  return zones.map((source) => {
    let previous: NormalizedTick | null = null;
    for (const tick of ticks) {
      if (tick.session !== "afternoon") continue;
      if (previous) {
        const support = previous.lastPrice > source.upper && tick.lastPrice <= source.upper;
        const resistance = previous.lastPrice < source.lower && tick.lastPrice >= source.lower;
        if (support || resistance) {
          const barIndex = indices.get(tick.barKey) ?? 0;
          const fraction = (tick.millisOfDay % 60000) / 60000;
          return {
            ...source,
            role: support ? "support" : "resistance",
            firstTouchTime: tick.eventTime,
            firstTouchX: barIndex + fraction,
          };
        }
      }
      previous = tick;
    }
    return { ...source, role: "neutral", firstTouchTime: null, firstTouchX: null };
  });
}

export async function analyzeFile(path: string, tradingDay: string, instrument: string): Promise<ChartPayload> {
  const meta = contractMeta(instrument);
  const rawTicks = await readRawTicks(path);
  const normalized = normalizeTicks(rawTicks, tradingDay, instrument);
  const { bars, indices } = buildBars(normalized.ticks);
  const profile = buildProfile(normalized.ticks, meta.tickSize);
  const zones = addTouchRoles(profile.zones, normalized.ticks, indices, bars);
  const ticks = [];
  const vwap = [];
  let weighted = 0;
  let volume = 0;
  for (const tick of normalized.ticks) {
    const barIndex = indices.get(tick.barKey) ?? 0;
    const fraction = (tick.millisOfDay % 60000) / 60000;
    const x = barIndex + fraction;
    ticks.push({ x, price: tick.lastPrice, time: tick.eventTime, deltaVolume: tick.deltaVolume });
    if (tick.deltaVolume > 0) {
      weighted += tick.lastPrice * tick.deltaVolume;
      volume += tick.deltaVolume;
      vwap.push({ x, price: weighted / volume });
    }
  }
  const prices = [
    ...bars.flatMap((bar) => [bar.low, bar.high]),
    ...profile.bins.map((bin) => bin.price),
    ...zones.flatMap((zone) => [zone.lower, zone.upper]),
  ];
  const low = prices.length > 0 ? Math.min(...prices) : 0;
  const high = prices.length > 0 ? Math.max(...prices) : 1;
  const padding = Math.max(meta.tickSize * 2, (high - low) * 0.04);
  return {
    algorithmVersion: "mvp-1",
    tradingDay,
    instrument,
    root: meta.root,
    tickSize: meta.tickSize,
    bars,
    ticks,
    vwap,
    profile: profile.bins,
    zones,
    priceMin: low - padding,
    priceMax: high + padding,
    quality: normalized.quality,
  };
}
