import Fastify, { type FastifyInstance, type FastifyReply } from "fastify";
import { createReadStream } from "node:fs";
import { readFile, stat } from "node:fs/promises";
import { extname, join } from "node:path";
import { loadConfig } from "./config.js";
import { assertDate, visualPath } from "./paths.js";
import { ReviewStore } from "./reviews.js";
import { DataScanner } from "./scanner.js";
import { TaskConflictError, TaskManager } from "./tasks.js";
import type { AppConfig, PipelineRequestStep, ReviewInput } from "./types.js";

const CONTENT_TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
};
const GRADES = new Set(["", "A", "B", "C", "D"]);
const BEHAVIORS = new Set(["", "clean_bounce", "weak_bounce", "immediate_break", "choppy", "no_touch", "data_problem"]);
const QUALITIES = new Set(["", "good", "fair", "poor"]);
const RUN_STEPS = new Set<PipelineRequestStep>(["1min", "profile", "visual", "all"]);

function validateReview(value: unknown): ReviewInput {
  if (!value || typeof value !== "object") throw new Error("Invalid review body");
  const body = value as Record<string, unknown>;
  const manualGrade = typeof body.manualGrade === "string" ? body.manualGrade : "";
  const manualBehavior = typeof body.manualBehavior === "string" ? body.manualBehavior : "";
  const profileQuality = typeof body.profileQuality === "string" ? body.profileQuality : "";
  const zoneQuality = typeof body.zoneQuality === "string" ? body.zoneQuality : "";
  const comment = typeof body.comment === "string" ? body.comment.trim() : "";
  if (!GRADES.has(manualGrade)) throw new Error("Invalid manualGrade");
  if (!BEHAVIORS.has(manualBehavior)) throw new Error("Invalid manualBehavior");
  if (!QUALITIES.has(profileQuality) || !QUALITIES.has(zoneQuality)) throw new Error("Invalid quality");
  if (comment.length > 4000) throw new Error("Comment is too long");
  return {
    manualGrade: manualGrade as ReviewInput["manualGrade"],
    manualBehavior: manualBehavior as ReviewInput["manualBehavior"],
    profileQuality: profileQuality as ReviewInput["profileQuality"],
    zoneQuality: zoneQuality as ReviewInput["zoneQuality"],
    comment,
  };
}

async function sendFile(reply: FastifyReply, path: string): Promise<FastifyReply> {
  const info = await stat(path);
  if (!info.isFile()) return reply.code(404).send({ error: "Not found" });
  reply.type(CONTENT_TYPES[extname(path).toLowerCase()] ?? "application/octet-stream");
  return reply.send(createReadStream(path));
}

export function buildApp(overrides: Partial<AppConfig> = {}): FastifyInstance {
  const config = loadConfig(overrides);
  const app = Fastify({ logger: false, bodyLimit: 256 * 1024 });
  const reviews = new ReviewStore(config.dbPath);
  const scanner = new DataScanner(config, reviews);
  const tasks = new TaskManager(config);
  const publicRoot = join(config.repoRoot, "tools", "market_profile_web", "public");

  app.addHook("onClose", async () => reviews.close());
  app.setErrorHandler((error, _request, reply) => {
    if (error instanceof TaskConflictError) return reply.code(409).send({ error: error.message });
    const code = (error as NodeJS.ErrnoException).code === "ENOENT" ? 404 : 400;
    return reply.code(code).send({ error: error instanceof Error ? error.message : String(error) });
  });

  app.get("/", async (_request, reply) => sendFile(reply, join(publicRoot, "index.html")));
  app.get("/app.js", async (_request, reply) => sendFile(reply, join(publicRoot, "app.js")));
  app.get("/styles.css", async (_request, reply) => sendFile(reply, join(publicRoot, "styles.css")));

  app.get("/api/health", async () => ({ ok: true, host: config.host, dataRoot: config.dataRoot }));
  app.get("/api/config", async () => ({ dataRoot: config.dataRoot, host: config.host, port: config.port }));
  app.get("/api/days", async () => scanner.listDays());
  app.get("/api/days/:date", async (request) => {
    const { date } = request.params as { date: string };
    return {
      day: await scanner.getDay(assertDate(date)),
      instruments: await scanner.listInstruments(date),
      tasks: tasks.list(date),
    };
  });
  app.get("/api/days/:date/instruments", async (request) => {
    const { date } = request.params as { date: string };
    return scanner.listInstruments(assertDate(date));
  });
  app.get("/api/days/:date/instruments/:instrument/levels", async (request) => {
    const { date, instrument } = request.params as { date: string; instrument: string };
    return scanner.levels(assertDate(date), instrument);
  });
  app.put("/api/days/:date/reviews/:zoneId", async (request) => {
    const { date, zoneId } = request.params as { date: string; zoneId: string };
    const body = request.body as { instrument?: unknown } & Record<string, unknown>;
    const instrument = typeof body?.instrument === "string" ? body.instrument : "";
    const zones = await scanner.levels(assertDate(date), instrument);
    const zone = zones.find((item) => item.zoneId === zoneId);
    if (!zone) throw new Error("Zone not found");
    return reviews.upsert(
      {
        tradingDay: zone.tradingDay,
        instrument: zone.instrument,
        zoneId: zone.zoneId,
        nodeType: zone.nodeType,
        center: zone.center,
        lower: zone.lower,
        upper: zone.upper,
        autoOutcome: zone.autoOutcome,
      },
      validateReview(body),
    );
  });
  app.delete("/api/days/:date/reviews/:zoneId", async (request) => {
    const { date, zoneId } = request.params as { date: string; zoneId: string };
    const body = request.body as { instrument?: unknown };
    const instrument = typeof body?.instrument === "string" ? body.instrument : "";
    const zones = await scanner.levels(assertDate(date), instrument);
    if (!zones.some((item) => item.zoneId === zoneId)) throw new Error("Zone not found");
    return { deleted: reviews.delete(date, instrument, zoneId) };
  });
  app.get("/api/days/:date/reviews.csv", async (request, reply) => {
    const { date } = request.params as { date: string };
    const validDate = assertDate(date);
    reply.type("text/csv; charset=utf-8");
    reply.header("Content-Disposition", `attachment; filename=${validDate}-reviews.csv`);
    return reply.send(reviews.exportCsv(validDate));
  });

  app.post("/api/days/:date/run", async (request, reply) => {
    const { date } = request.params as { date: string };
    const body = request.body as { step?: unknown };
    if (typeof body?.step !== "string" || !RUN_STEPS.has(body.step as PipelineRequestStep)) {
      throw new Error("Invalid pipeline step");
    }
    return reply.code(202).send(tasks.start(assertDate(date), body.step as PipelineRequestStep));
  });
  app.get("/api/days/:date/tasks", async (request) => {
    const { date } = request.params as { date: string };
    return tasks.list(assertDate(date));
  });
  app.get("/api/tasks/:id", async (request, reply) => {
    const { id } = request.params as { id: string };
    const task = tasks.get(id);
    return task ?? reply.code(404).send({ error: "Task not found" });
  });
  app.get("/api/tasks/:id/events", async (request, reply) => {
    const { id } = request.params as { id: string };
    const initial = tasks.get(id);
    if (!initial) return reply.code(404).send({ error: "Task not found" });
    reply.hijack();
    reply.raw.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    });
    const send = (task: unknown) => reply.raw.write(`data: ${JSON.stringify(task)}\n\n`);
    send(initial);
    const unsubscribe = tasks.subscribe(id, (task) => {
      send(task);
      if (task.status === "succeeded" || task.status === "failed") {
        unsubscribe();
        reply.raw.end();
      }
    });
    request.raw.on("close", unsubscribe);
  });

  app.get("/data/:date/visual/*", async (request, reply) => {
    const params = request.params as { date: string; "*": string };
    return sendFile(reply, visualPath(config.dataRoot, params.date, params["*"]));
  });

  return app;
}
