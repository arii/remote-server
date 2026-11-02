import briefingFixture from '../mocks/briefing.json';
import jobsFixture from '../mocks/jobs.json';
import ledgerFixture from '../mocks/ledger.json';
import type {
  BriefingResponse,
  JobsResponse,
  LedgerResponse,
} from '../apiTypes';

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
