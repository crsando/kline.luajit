import { homedir } from "node:os";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import type { AppConfig } from "./types.js";

const sourceDir = dirname(fileURLToPath(import.meta.url));
const packageRoot = sourceDir.includes(`${sep}dist${sep}`)
  ? resolve(sourceDir, "../..")
  : resolve(sourceDir, "..");
const defaultRepoRoot = resolve(packageRoot, "../..");

function integer(value: string | undefined, fallback: number): number {
  if (!value) return fallback;
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`Invalid port: ${value}`);
  }
  return parsed;
}

export function loadConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  const host = overrides.host ?? process.env.KLINE_HOST ?? "127.0.0.1";
  if (host !== "127.0.0.1" && host !== "localhost") {
    throw new Error("KLINE_HOST must be 127.0.0.1 or localhost");
  }
  return {
    host,
    port: overrides.port ?? integer(process.env.KLINE_PORT, 4317),
    dataRoot: resolve(overrides.dataRoot ?? process.env.KLINE_DATA_ROOT ?? join(homedir(), "Downloads")),
    repoRoot: resolve(overrides.repoRoot ?? process.env.KLINE_REPO_ROOT ?? defaultRepoRoot),
    dbPath: resolve(
      overrides.dbPath ??
        process.env.KLINE_REVIEW_DB ??
        join(homedir(), "Library", "Application Support", "kline-market-profile", "reviews.sqlite"),
    ),
    python: overrides.python ?? process.env.KLINE_PYTHON ?? "python3",
  };
}
