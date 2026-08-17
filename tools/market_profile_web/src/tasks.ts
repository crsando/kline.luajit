import { EventEmitter } from "node:events";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { assertDate, dayPath } from "./paths.js";
import type {
  AppConfig,
  PipelineRequestStep,
  PipelineStep,
  TaskLog,
  TaskRecord,
} from "./types.js";

const ALL_STEPS: PipelineStep[] = ["1min", "profile", "visual"];

export class TaskConflictError extends Error {}

export class TaskManager {
  private readonly tasks = new Map<string, TaskRecord>();
  private readonly activeByDate = new Map<string, string>();
  private readonly events = new EventEmitter();
  private seq = 0;

  constructor(readonly config: AppConfig) {
    this.events.setMaxListeners(200);
  }

  start(date: string, requestedStep: PipelineRequestStep): TaskRecord {
    assertDate(date);
    if (this.activeByDate.has(date)) {
      throw new TaskConflictError(`A pipeline task is already running for ${date}`);
    }
    const steps = requestedStep === "all" ? [...ALL_STEPS] : [requestedStep];
    const task: TaskRecord = {
      id: randomUUID(),
      date,
      requestedStep,
      steps,
      status: "queued",
      currentStep: null,
      startedAt: null,
      finishedAt: null,
      exitCode: null,
      logs: [],
    };
    this.tasks.set(task.id, task);
    this.activeByDate.set(date, task.id);
    this.log(task, "system", `Queued ${steps.join(" -> ")}`);
    void this.execute(task);
    return this.snapshot(task);
  }

  get(id: string): TaskRecord | null {
    const task = this.tasks.get(id);
    return task ? this.snapshot(task) : null;
  }

  list(date?: string): TaskRecord[] {
    return [...this.tasks.values()]
      .filter((task) => !date || task.date === date)
      .sort((a, b) => (b.startedAt ?? "").localeCompare(a.startedAt ?? ""))
      .map((task) => this.snapshot(task));
  }

  subscribe(id: string, listener: (task: TaskRecord) => void): () => void {
    const event = `task:${id}`;
    this.events.on(event, listener);
    return () => this.events.off(event, listener);
  }

  private async execute(task: TaskRecord): Promise<void> {
    task.status = "running";
    task.startedAt = new Date().toISOString();
    this.emit(task);
    try {
      for (const step of task.steps) {
        task.currentStep = step;
        this.log(task, "system", `Starting ${step}`);
        const exitCode = await this.runStep(task, step);
        if (exitCode !== 0) {
          task.status = "failed";
          task.exitCode = exitCode;
          this.log(task, "system", `${step} failed with exit code ${exitCode}`);
          return;
        }
        this.log(task, "system", `${step} completed`);
      }
      task.status = "succeeded";
      task.exitCode = 0;
      this.log(task, "system", "Pipeline completed");
    } catch (error) {
      task.status = "failed";
      task.exitCode = -1;
      this.log(task, "stderr", error instanceof Error ? error.message : String(error));
    } finally {
      task.currentStep = null;
      task.finishedAt = new Date().toISOString();
      this.activeByDate.delete(task.date);
      this.emit(task);
    }
  }

  private async runStep(task: TaskRecord, step: PipelineStep): Promise<number> {
    const root = dayPath(this.config.dataRoot, task.date);
    const profile = join(root, "day_market_profile");
    const commands: Record<PipelineStep, string[]> = {
      "1min": [
        join(this.config.repoRoot, "tools", "tick_data", "convert_to_1m.py"),
        root,
        join(root, "1min"),
        "--workers",
        "8",
      ],
      profile: [
        join(this.config.repoRoot, "tools", "market_profile", "market_profile.py"),
        root,
        profile,
        "--overwrite",
      ],
      visual: [
        join(this.config.repoRoot, "tools", "market_profile", "visualize.py"),
        profile,
        join(profile, "visual"),
        "--overwrite",
      ],
    };
    if (step === "1min") await mkdir(join(root, "1min"), { recursive: true });
    return new Promise<number>((resolve, reject) => {
      const args = commands[step];
      const child = spawn(this.config.python, args, {
        cwd: this.config.repoRoot,
        env: { ...process.env, PYTHONUNBUFFERED: "1" },
        shell: false,
      });
      child.stdout.setEncoding("utf8");
      child.stderr.setEncoding("utf8");
      child.stdout.on("data", (chunk: string) => this.logChunk(task, "stdout", chunk));
      child.stderr.on("data", (chunk: string) => this.logChunk(task, "stderr", chunk));
      child.on("error", reject);
      child.on("close", (code) => resolve(code ?? -1));
    });
  }

  private logChunk(task: TaskRecord, stream: "stdout" | "stderr", chunk: string): void {
    const lines = chunk.replace(/\r/g, "").split("\n").filter(Boolean);
    for (const line of lines) this.log(task, stream, line);
  }

  private log(task: TaskRecord, stream: TaskLog["stream"], message: string): void {
    task.logs.push({ seq: ++this.seq, time: new Date().toISOString(), stream, message });
    if (task.logs.length > 5000) task.logs.splice(0, task.logs.length - 5000);
    this.emit(task);
  }

  private emit(task: TaskRecord): void {
    this.events.emit(`task:${task.id}`, this.snapshot(task));
  }

  private snapshot(task: TaskRecord): TaskRecord {
    return { ...task, steps: [...task.steps], logs: task.logs.map((log) => ({ ...log })) };
  }
}
