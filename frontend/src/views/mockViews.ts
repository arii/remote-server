import briefingFixture from '../mocks/briefing.json';
import jobsFixture from '../mocks/jobs.json';
import ledgerFixture from '../mocks/ledger.json';
import historyFixture from '../mocks/history.json';
import systemHealthFixture from '../mocks/system-health.json';
import workflowHealthFixture from '../mocks/workflow-health.json';
import type {
  BriefingResponse,
  JobsResponse,
  LedgerResponse,
} from '../apiTypes';
import type {
  SystemHealthResponse,
  WorkflowHealthResponse,
} from '../apiClient';

// Offline dev override: append ?mock=1 to use bundled fixtures instead of
// live endpoints. Views always attempt the live fetch first and fail visibly
// on 404; they only use these loaders when ?mock=1 is present.
export function useMock(): boolean {
  return (
    typeof window !== 'undefined' &&
    new URLSearchParams(window.location.search).get('mock') === '1'
  );
}

export function loadBriefing(): BriefingResponse {
  return { ...(briefingFixture as BriefingResponse) };
}

export function loadJobs(): JobsResponse {
  const j = jobsFixture as JobsResponse;
  return { jobs: [...j.jobs] };
}

export function loadLedger(): LedgerResponse {
  const l = ledgerFixture as LedgerResponse;
  return { entries: [...l.entries] };
}

// Unified history fixture: includes a repeat-failure incident run
// (webhook fast-forward conflict ×3) so ?mock=1 exercises collapse.
export function loadHistory(): { jobs: JobsResponse['jobs']; entries: LedgerResponse['entries'] } {
  const h = historyFixture as unknown as { jobs: JobsResponse['jobs']; entries: LedgerResponse['entries'] };
  return { jobs: [...h.jobs], entries: [...h.entries] };
}

export function loadSystemHealth(): SystemHealthResponse {
  return { ...(systemHealthFixture as SystemHealthResponse) };
}

export function loadWorkflowHealth(): WorkflowHealthResponse {
  const w = workflowHealthFixture as WorkflowHealthResponse;
  return { ...w, workflows: [...w.workflows] };
}
