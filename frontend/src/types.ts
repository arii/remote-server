export interface SessionRecord {
  file: string;
  id?: string;
  mtime: number;
  cwd: string;
  repo: string;
  title: string;
  kind?: string;
  source?: string;
  done?: boolean;
}

export interface ListResponse {
  sessions: SessionRecord[];
}

export interface Turn {
  role: string;
  text: string;
  timestamp?: unknown;
  tool_calls?: Array<{ name: string; input?: object; args?: object }>;
  tool_results?: Array<{ name: string; content: string }>;
}

export interface DetailResponse {
  file: string;
  id?: string;
  cwd: string;
  repo: string;
  started?: number | string;
  source?: string;
  turns: Turn[];
  total_entries?: number;
  raw_output?: string;
  output?: string;
  error?: string;
}

export interface HealthStatus {
  ok: boolean;
  sessions: number;
  timestamp: number;
}
