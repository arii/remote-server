import { useEffect, useState } from 'react';
import { clearJobs, fetchJobs, runJob } from '../apiClient';
import type { JobsResponse } from '../apiTypes';
import { loadJobs, useMock } from './mockViews';

export default function JobsView() {
  const [data, setData] = useState<JobsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const live = useMock() ? loadJobs() : await fetchJobs();
        if (!cancelled) setData(live);
      } catch (err) {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (error)
    return (
      <div data-testid="jobs-error" role="alert">
        Jobs unavailable: {error}
      </div>
    );
  if (!data) return <div data-testid="jobs-loading">Loading jobs…</div>;
  return (
    <section data-testid="jobs-view">
      <ul className="session-list">
        {data.jobs.map((j) => (
          <li key={j.id}>
            <span className="row-title">{j.title}</span>
            <span className="row-meta">
              {j.id} · {j.status}
            </span>{' '}
            <button
              type="button"
              onClick={() => {
                void runJob(j.id);
              }}
            >
              Run
            </button>
          </li>
        ))}
      </ul>
      <div className="actions" role="group" aria-label="Job actions">
        <button
          type="button"
          onClick={() => {
            void clearJobs();
          }}
        >
          Clear finished
        </button>
      </div>
    </section>
  );
}
