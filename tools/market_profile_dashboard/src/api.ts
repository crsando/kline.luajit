import Fastify, { type FastifyInstance, type FastifyReply } from "fastify";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { extname } from "node:path";
import { APP_CONFIG, contractMeta } from "./config.js";
import { Catalog } from "./catalog.js";
import { analyzeFile } from "./engine.js";
import { assertDate, assertInstrument } from "./paths.js";
import type { CatalogEntry, ChartPayload } from "./types.js";

const CONTENT_TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};

interface CacheRecord {
  fileSize: number;
  modifiedAt: string;
  result: ChartPayload;
}

async function sendPublicFile(reply: FastifyReply, path: string): Promise<FastifyReply> {
  const info = await stat(path);
  if (!info.isFile()) return reply.code(404).send({ error: "Not found" });
  reply.type(CONTENT_TYPES[extname(path).toLowerCase()] ?? "application/octet-stream");
  return reply.send(createReadStream(path));
}

export function buildApp(): FastifyInstance {
  const app = Fastify({ logger: false, bodyLimit: 128 * 1024 });
  const catalog = new Catalog();
  const cache = new Map<string, CacheRecord>();

  app.setErrorHandler((error, _request, reply) => {
    const code = (error as NodeJS.ErrnoException).code === "ENOENT" ? 404 : 400;
    return reply.code(code).send({ error: error instanceof Error ? error.message : String(error) });
  });

  app.get("/", async (_request, reply) => sendPublicFile(reply, `${APP_CONFIG.publicRoot}/index.html`));
  app.get("/app.js", async (_request, reply) => sendPublicFile(reply, `${APP_CONFIG.publicRoot}/app.js`));
  app.get("/styles.css", async (_request, reply) => sendPublicFile(reply, `${APP_CONFIG.publicRoot}/styles.css`));
  app.get("/vendor/echarts.min.js", async (_request, reply) => (
    sendPublicFile(reply, `${APP_CONFIG.publicRoot}/vendor/echarts.min.js`)
  ));

  app.get("/api/health", async () => ({
    ok: true,
    algorithmVersion: APP_CONFIG.algorithmVersion,
    cachedCharts: cache.size,
  }));

  app.get("/api/days", async () => catalog.listDays());

  app.get("/api/days/:date/instruments", async (request) => {
    const { date } = request.params as { date: string };
    const entries = await catalog.listInstruments(assertDate(date));
    return entries.map(({ path: _path, ...entry }) => entry);
  });

  app.get("/api/days/:date/instruments/:instrument/chart", async (request) => {
    const { date: rawDate, instrument: rawInstrument } = request.params as { date: string; instrument: string };
    const date = assertDate(rawDate);
    const instrument = assertInstrument(rawInstrument);
    contractMeta(instrument);
    const entry = await catalog.get(date, instrument);
    const key = `${date}:${instrument}`;
    const cached = cache.get(key);
    if (cached && cached.fileSize === entry.fileSize && cached.modifiedAt === entry.modifiedAt) return cached.result;
    const result = await analyzeFile(entry.path, date, instrument);
    cache.set(key, { fileSize: entry.fileSize, modifiedAt: entry.modifiedAt, result });
    while (cache.size > 16) cache.delete(cache.keys().next().value as string);
    return result;
  });

  return app;
}

export function catalogEntryForTest(entry: CatalogEntry): string {
  return `${entry.date}:${entry.instrument}`;
}
