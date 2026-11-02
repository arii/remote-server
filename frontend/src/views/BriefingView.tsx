import { useEffect, useState } from 'react';
import { fetchBriefing } from '../apiClient';
import type { BriefingResponse } from '../apiTypes';
import { loadBriefing, useMock } from './mockViews';

export default function BriefingView({ repo }: { repo: string }) {
  const [data, setData] = useState<BriefingResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const live = useMock() ? loadBriefing() : await fetchBriefing(repo);
        if (!cancelled) setData({ ...live, repo });
      } catch (err) {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [repo]);

  if (error)
    return (
      <div data-testid="briefing-error" role="alert">
        Briefing unavailable: {error}
      </div>
    );
  if (!data) return <div data-testid="briefing-loading">Loading briefing…</div>;
  return (
    <section data-testid="briefing-view" className="detail">
      <h2>Briefing — {data.repo}</h2>
      <p className="row-meta">{data.summary ?? 'No summary.'}</p>
      <p className="row-meta">
        Open PRs: {data.open_prs?.length ?? 0}
      </p>
    </section>
  );
}
