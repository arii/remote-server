import briefingFixture from '../mocks/briefing.json';
import historyFixture from '../mocks/history.json';
import jobsFixture from '../mocks/jobs.json';
import dispatchFixture from '../mocks/dispatch.json';
import chatFixture from '../mocks/chat.json';
import systemHealthFixture from '../mocks/system-health.json';
import systemStatsFixture from '../mocks/system-stats.json';
import repoDiffFixture from '../mocks/repo-diff.json';
import workflowHealthFixture from '../mocks/workflow-health.json';
import type {
  BriefingResponse,
  DispatchQueueResponse,
  JobsResponse,
  LedgerResponse,
} from '../apiTypes';
import type {
  ContainerStatsResponse,
  RepoDiffResponse,
  SystemHealthResponse,
  WorkflowHealthResponse,
} from '../apiClient';
import reviewFixture from '../mocks/review.json';
import type {
  IssueRow,
  PrDetailsResponse,
  PrRow,
  RepoInfo,
} from '../reviewTypes';

// Offline dev override: append ?mock=1 to use bundled fixtures instead of
// live endpoints. Views always attempt the live fetch first and fail visibly
// on 404; they only use these loaders when ?mock=1 is present.
export function useMock(): boolean {
  return (
    typeof window !== 'undefined' &&
    new URLSearchParams(window.location.search).get('mock') === '1'
  );
}

// Unified history fixture: includes a repeat-failure incident run
// (webhook fast-forward conflict ×3) so ?mock=1 exercises collapse.
export function loadBriefing(): BriefingResponse {
  const b = briefingFixture as unknown as BriefingResponse;
  return { ...b, attention: [...b.attention] };
}

export function loadHistory(): { jobs: JobsResponse['jobs']; entries: LedgerResponse['entries'] } {
  const h = historyFixture as unknown as { jobs: JobsResponse['jobs']; entries: LedgerResponse['entries'] };
  return { jobs: [...h.jobs], entries: [...h.entries] };
}

export function loadSystemHealth(): SystemHealthResponse {
  return { ...(systemHealthFixture as SystemHealthResponse) };
}

export function loadContainerStats(): ContainerStatsResponse {
  const s = systemStatsFixture as unknown as ContainerStatsResponse;
  return { containers: [...s.containers] };
}

export function loadRepoDiff(repo: string): RepoDiffResponse {
  return { ...(repoDiffFixture as unknown as RepoDiffResponse), repo };
}

export function loadRepos(): RepoInfo[] {
  const r = reviewFixture as unknown as { repos: RepoInfo[] };
  return [...r.repos];
}

export function loadPrs(): PrRow[] {
  const r = reviewFixture as unknown as { prs: PrRow[] };
  return [...r.prs];
}

export function loadPrDetails(): PrDetailsResponse {
  const r = reviewFixture as unknown as { details: PrDetailsResponse };
  return { ...r.details, files: [...r.details.files] };
}

export function loadIssues(): IssueRow[] {
  const r = reviewFixture as unknown as { issues: IssueRow[] };
  return [...r.issues];
}

export function loadWorkflowHealth(): WorkflowHealthResponse {
  const w = workflowHealthFixture as WorkflowHealthResponse;
  return { ...w, workflows: [...w.workflows] };
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  text: string;
}

export function loadJobs(): JobsResponse {
  const j = jobsFixture as unknown as JobsResponse;
  return { jobs: [...j.jobs] };
}

export function loadDispatchQueue(): DispatchQueueResponse {
  const q = dispatchFixture as unknown as DispatchQueueResponse;
  return { items: [...q.items] };
}

export function loadChat(): { messages: ChatMessage[]; progress: { done: boolean; output: string } } {
  const c = chatFixture as unknown as { messages: ChatMessage[]; progress: { done: boolean; output: string } };
  return { messages: [...c.messages], progress: { ...c.progress } };
}
