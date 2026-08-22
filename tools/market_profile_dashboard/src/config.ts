import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export type SessionName = "morning" | "afternoon";

export interface SessionRange {
  start: number;
  end: number;
}

export interface ContractMeta {
  root: string;
  tickSize: number;
  sessions: Record<SessionName, SessionRange[]>;
}

const day = (ranges: Array<[string, string]>): SessionRange[] => ranges.map(([start, end]) => ({
  start: toMinute(start),
  end: toMinute(end),
}));

const moduleDir = dirname(fileURLToPath(import.meta.url));
const publicRoot = existsSync(resolve(moduleDir, "../public"))
  ? resolve(moduleDir, "../public")
  : resolve(moduleDir, "../../public");

function toMinute(value: string): number {
  const [hour, minute] = value.split(":").map(Number);
  return hour * 60 + minute;
}

const indexSessions = {
  morning: day([["09:30", "11:30"]]),
  afternoon: day([["13:00", "15:00"]]),
};

const bondSessions = {
  morning: day([["09:30", "11:30"]]),
  afternoon: day([["13:00", "15:15"]]),
};

const commoditySessions = {
  morning: day([["09:00", "10:15"], ["10:30", "11:30"]]),
  afternoon: day([["13:30", "15:00"]]),
};

export const APP_CONFIG = {
  host: "0.0.0.0",
  port: 4317,
  dataRoot: "/srv/kline/canonical/test/tick",
  algorithmVersion: "mvp-1",
  publicRoot,
  contracts: {
    IF: { root: "IF", tickSize: 0.2, sessions: indexSessions },
    IC: { root: "IC", tickSize: 0.2, sessions: indexSessions },
    IH: { root: "IH", tickSize: 0.2, sessions: indexSessions },
    IM: { root: "IM", tickSize: 0.2, sessions: indexSessions },
    T: { root: "T", tickSize: 0.005, sessions: bondSessions },
    TF: { root: "TF", tickSize: 0.005, sessions: bondSessions },
    TS: { root: "TS", tickSize: 0.002, sessions: bondSessions },
    TL: { root: "TL", tickSize: 0.01, sessions: bondSessions },
    LH: { root: "LH", tickSize: 5, sessions: commoditySessions },
  } satisfies Record<string, ContractMeta>,
} as const;

export type AppConfig = typeof APP_CONFIG;

export function contractMeta(instrument: string): ContractMeta {
  const root = instrument.match(/^[A-Za-z]+/)?.[0].toUpperCase();
  if (!root) throw new Error(`Invalid instrument: ${instrument}`);
  const meta = APP_CONFIG.contracts[root as keyof typeof APP_CONFIG.contracts];
  if (!meta) throw new Error(`Unsupported contract root: ${root}`);
  return meta;
}
