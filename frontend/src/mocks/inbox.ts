import type { InboxFeed } from '../apiClient';
import { loadAttention, loadAttentionEmpty } from './attention';
import { loadJobs } from '../views/mockViews';

function runsFromJobs(): InboxFeed['runs'] {
  const jobs = loadJobs().jobs;
  const active = jobs.find((j) => !j.done);
  const recent = jobs
    .filter((j) => j.done)
    .slice(0, 5)
    .map((j) => ({
      id: j.id,
      action: j.name ?? j.title ?? j.id,
      exit: j.exit_code ?? null,
    }));
  return {
    active: active
      ? {
          id: active.id,
          action: active.name ?? active.title ?? active.id,
          started: active.created_at ?? active.at ?? null,
        }
      : null,
    recent,
  };
}

export function loadInbox(): InboxFeed {
  const attention = loadAttention();
  return {
    repo: 'remote-server',
    updated_at: attention.updated_at,
    attention: attention.items,
    merge_ready: {
      count: 1,
      top: [{ number: 12, title: 'Fix it' }],
    },
    runs: runsFromJobs(),
  };
}

export function loadInboxEmpty(): InboxFeed {
  const attention = loadAttentionEmpty();
  return {
    repo: 'remote-server',
    updated_at: attention.updated_at,
    attention: attention.items,
    merge_ready: { count: 0, top: [] },
    runs: { active: null, recent: [] },
  };
}
