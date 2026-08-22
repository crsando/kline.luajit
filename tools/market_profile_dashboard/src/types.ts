import type { ContractMeta, SessionName } from "./config.js";

export interface RawTick {
  eventTime: string;
  timestamp: number;
  symbol: string;
  lastPrice: number;
  cumulativeVolume: number;
}

export interface NormalizedTick extends RawTick {
  tradingDay: string;
  millisOfDay: number;
  session: SessionName;
  deltaVolume: number;
  qualityFlags: string[];
  barKey: string;
}

export interface Bar {
  index: number;
  time: string;
  session: SessionName;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  tickCount: number;
}

export interface TickPoint {
  x: number;
  price: number;
  time: string;
  deltaVolume: number;
}

export interface VwapPoint {
  x: number;
  price: number;
}

export type ZoneType = "VPOC" | "VAL" | "VAH";
export type ZoneRole = "support" | "resistance" | "neutral";

export interface ProfileBin {
  price: number;
  volume: number;
  volumeShare: number;
}

export interface Zone {
  type: ZoneType;
  center: number;
  lower: number;
  upper: number;
  role: ZoneRole;
  firstTouchTime: string | null;
  firstTouchX: number | null;
}

export interface QualityStats {
  rawTicks: number;
  validTicks: number;
  outsideSessionTicks: number;
  backwardsTicks: number;
  invalidRows: number;
  volumeResets: number;
  zeroDeltaTicks: number;
  morningTicks: number;
  afternoonTicks: number;
}

export interface CatalogEntry {
  date: string;
  instrument: string;
  root: string;
  tickSize: number;
  path: string;
  fileSize: number;
  modifiedAt: string;
}

export interface ChartPayload {
  algorithmVersion: string;
  tradingDay: string;
  instrument: string;
  root: string;
  tickSize: number;
  bars: Bar[];
  ticks: TickPoint[];
  vwap: VwapPoint[];
  profile: ProfileBin[];
  zones: Zone[];
  priceMin: number;
  priceMax: number;
  quality: QualityStats;
}

export type AnalysisConfig = ContractMeta;
