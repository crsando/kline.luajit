import type { ContractMeta, SessionName } from "./config.js";

export function sessionFor(millisOfDay: number, meta: ContractMeta): SessionName | null {
  const minute = Math.floor(millisOfDay / 60000);
  for (const session of ["morning", "afternoon"] as const) {
    if (meta.sessions[session].some((range) => range.start <= minute && minute < range.end)) return session;
  }
  return null;
}
