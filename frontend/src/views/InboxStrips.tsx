import type { InboxFeed } from '../apiClient';

// Compact actionable strips between the attention feed and the
// Review/Jobs sections: merge-ready PRs and the active run. Purely
// presentational; the parent owns section mounting.
export default function InboxStrips({
  feed,
  onOpenReview,
  onOpenJobs,
}: {
  feed: InboxFeed;
  onOpenReview: () => void;
  onOpenJobs: () => void;
}) {
  const ready = feed.merge_ready;
  const active = feed.runs.active;
  if (ready.count === 0 && !active) return null;
  return (
    <div className="actions" data-testid="inbox-strips">
      {ready.count > 0 && (
        <button type="button" data-testid="inbox-merge-ready" onClick={onOpenReview}>
          Ready to merge ({ready.count}
          {ready.top.length > 0 ? `): ${ready.top.map((p) => `#${p.number} ${p.title}`).join(', ')}` : ')'}
        </button>
      )}
      {active && (
        <button type="button" data-testid="inbox-active-run" onClick={onOpenJobs}>
          Active: {active.action}
        </button>
      )}
    </div>
  );
}
