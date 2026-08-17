import { extname, resolve, sep } from "node:path";

const DATE_RE = /^\d{8}$/;
const VISUAL_EXTENSIONS = new Set([".html", ".js", ".css", ".json", ".png", ".svg", ".txt"]);

export function assertDate(value: string): string {
  if (!DATE_RE.test(value)) throw new Error("Invalid trading date; expected YYYYMMDD");
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(4, 6));
  const day = Number(value.slice(6, 8));
  const candidate = new Date(Date.UTC(year, month - 1, day));
  if (
    candidate.getUTCFullYear() !== year ||
    candidate.getUTCMonth() !== month - 1 ||
    candidate.getUTCDate() !== day
  ) {
    throw new Error("Invalid calendar date");
  }
  return value;
}

export function dayPath(dataRoot: string, date: string): string {
  return resolveInside(dataRoot, assertDate(date));
}

export function visualPath(dataRoot: string, date: string, relativePath: string): string {
  const root = resolve(dayPath(dataRoot, date), "day_market_profile", "visual");
  const decoded = decodeURIComponent(relativePath || "index.html");
  if (decoded.includes("\0")) throw new Error("Invalid visual path");
  const target = resolveInside(root, decoded);
  if (!VISUAL_EXTENSIONS.has(extname(target).toLowerCase())) {
    throw new Error("Unsupported visual file type");
  }
  return target;
}

export function resolveInside(root: string, ...segments: string[]): string {
  const normalizedRoot = resolve(root);
  const target = resolve(normalizedRoot, ...segments);
  if (target !== normalizedRoot && !target.startsWith(normalizedRoot + sep)) {
    throw new Error("Path escapes configured root");
  }
  return target;
}
