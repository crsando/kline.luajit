export type PipelineStep = "1min" | "profile" | "visual";
export type PipelineRequestStep = PipelineStep | "all";
export type TaskStatus = "queued" | "running" | "succeeded" | "failed";

export interface AppConfig {
  host: string;
  port: number;
  dataRoot: string;
  repoRoot: string;
  dbPath: string;
  python: string;
}

export interface DaySummary {
  date: string;
  rawTickFiles: number;
  oneMinuteFiles: number;
  profileReady: boolean;
  visualReady: boolean;
  selectedContracts: number;
  levels: number;
  events: number;
  reviews: number;
}

export interface InstrumentSummary {
  instrument: string;
  root: string;
  exchange: string;
  tickSize: number;
  availableTime: string;
  detailUrl: string;
  imageUrl: string;
  stats: Record<string, number>;
  reviews: number;
}

export interface ZoneRecord {
  zoneId: string;
  tradingDay: string;
  instrument: string;
  nodeType: string;
  center: number;
  lower: number;
  upper: number;
  availableTime: string;
  role: string;
  autoOutcome: string;
  persistence: number;
  prominence: number;
  review: ReviewRecord | null;
}

export interface ReviewInput {
  manualGrade: "" | "A" | "B" | "C" | "D";
  manualBehavior: "" | "clean_bounce" | "weak_bounce" | "immediate_break" | "choppy" | "no_touch" | "data_problem";
  profileQuality: "" | "good" | "fair" | "poor";
  zoneQuality: "" | "good" | "fair" | "poor";
  comment: string;
}

export interface ReviewRecord extends ReviewInput {
  tradingDay: string;
  instrument: string;
  zoneId: string;
  nodeType: string;
  center: number;
  lower: number;
  upper: number;
  autoOutcome: string;
  createdAt: string;
  updatedAt: string;
}

export interface TaskLog {
  seq: number;
  time: string;
  stream: "system" | "stdout" | "stderr";
  message: string;
}

export interface TaskRecord {
  id: string;
  date: string;
  requestedStep: PipelineRequestStep;
  steps: PipelineStep[];
  status: TaskStatus;
  currentStep: PipelineStep | null;
  startedAt: string | null;
  finishedAt: string | null;
  exitCode: number | null;
  logs: TaskLog[];
}
