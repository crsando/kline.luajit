import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import type { AppConfig } from "../src/types.js";
import { TaskConflictError, TaskManager } from "../src/tasks.js";

async function waitFor(manager: TaskManager, id: string): Promise<ReturnType<TaskManager["get"]>> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const task = manager.get(id);
    if (task && ["succeeded", "failed"].includes(task.status)) return task;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error("task timeout");
}

test("runs all pipeline steps sequentially and blocks duplicate day tasks", async () => {
  const root = await mkdtemp(join(tmpdir(), "kline-task-"));
  const repo = join(root, "repo");
  const data = join(root, "data");
  const day = join(data, "20260109");
  await mkdir(join(repo, "tools", "tick_data"), { recursive: true });
  await mkdir(join(repo, "tools", "market_profile"), { recursive: true });
  await mkdir(day, { recursive: true });
  const script = "import time,sys\nprint('run', sys.argv[0], flush=True)\ntime.sleep(0.08)\n";
  await writeFile(join(repo, "tools", "tick_data", "convert_to_1m.py"), script);
  await writeFile(join(repo, "tools", "market_profile", "market_profile.py"), script);
  await writeFile(join(repo, "tools", "market_profile", "visualize.py"), script);
  const config: AppConfig = { host: "127.0.0.1", port: 4317, dataRoot: data, repoRoot: repo, dbPath: join(root, "db.sqlite"), python: "python3" };
  const manager = new TaskManager(config);
  try {
    const task = manager.start("20260109", "all");
    assert.throws(() => manager.start("20260109", "profile"), TaskConflictError);
    const done = await waitFor(manager, task.id);
    assert.equal(done?.status, "succeeded");
    assert.equal(done?.exitCode, 0);
    assert.ok(done?.logs.some((log) => log.message === "Starting 1min"));
    assert.ok(done?.logs.some((log) => log.message === "Starting profile"));
    assert.ok(done?.logs.some((log) => log.message === "Starting visual"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
