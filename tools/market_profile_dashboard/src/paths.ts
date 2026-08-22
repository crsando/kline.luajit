import { resolve, sep } from "node:path";

const DATE_RE = /^\d{8}$/;
const INSTRUMENT_RE = /^[A-Za-z][A-Za-z0-9]*$/;

export function assertDate(value: string): string {
  if (!DATE_RE.test(value)) throw new Error("Invalid trading date; expected YYYYMMDD");
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(4, 6));
  const day = Number(value.slice(6, 8));
  const candidate = new Date(Date.UTC(year, month - 1, day));
  if (
    candidate.getUTCFullYear() !== year
    || candidate.getUTCMonth() !== month - 1
    || candidate.getUTCDate() !== day
  ) throw new Error("Invalid calendar date");
  return value;
}

export function assertInstrument(value: string): string {
  if (!INSTRUMENT_RE.test(value)) throw new Error("Invalid instrument");
  return value;
}

export function resolveInside(root: string, ...segments: string[]): string {
  const normalizedRoot = resolve(root);
  const target = resolve(normalizedRoot, ...segments);
  if (target !== normalizedRoot && !target.startsWith(normalizedRoot + sep)) {
    throw new Error("Path escapes configured root");
  }
  return target;
}
