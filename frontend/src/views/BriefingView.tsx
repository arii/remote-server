import { useEffect, useState } from 'react';
import { fetchBriefing } from '../apiClient';
import type { BriefingResponse } from '../apiTypes';
import { loadBriefing, useMock } from './mockViews';

// Briefing lines come from the legacy briefing_summary passthrough and may
// carry reviewer HTML-comment markers or run long. Present the human part.
export function cleanBriefingLine(line: string): string {
  const stripped = line
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/ai-review-state:\s*\S+/g, ' ')
    .replace(/<!--[\s\S]*$/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return stripped.length > 280 ? `${stripped.slice(0, 277)}…` : stripped;
}

function Lines({ lines }: { lines: string[] }) {
  return (
    <ul className="session-list">
      {lines.map((line, i) => (
        // eslint-disable-next-line react/no-array-index-key
        <li key={i} className="detail">
          <p>{cleanBriefingLine(line)}</p>
        </li>
      ))}
    </ul>
  );
}

function Section({ title, lines, err }: { title: string; lines: string[]; err?: string }) {
  if (lines.length === 0 && !err) return null;
  return (
    <section>
      <h3>{title}</h3>
      {err && (
        <p className="row-meta" role="alert">
          {err}
        </p>
      )}
      <Lines lines={lines} />
    </section>
  );
}

function PrRow({ pr }: { pr: Record<string, unknown> }) {
  const meta = [pr.author, pr.branch, pr.ciLabel ?? pr.ciStatus]
    .map((v) => String(v ?? '').trim())
    .filter((v) => v.length > 0);
  return (
    <li className="detail">
      <span className="row-title">
        #{String(pr.number ?? '?')} {String(pr.title ?? 'Untitled')}
      </span>
      {meta.length > 0 && <p className="row-meta">{meta.join(' · ')}</p>}
    </li>
  );
}

export default function BriefingView({ repo }: { repo: string }) {
  const [data, setData] = useState<BriefingResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (useMock()) {
      setData(loadBriefing());
      return;
    }
    (async () => {
      try {
        setData(await fetchBriefing(repo));
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    })();
  }, [repo]);

  if (error) {
    return (
      <section data-testid="briefing-error" role="alert">
        Briefing unavailable — {error}
      </section>
    );
  }
  if (!data) return <section data-testid="briefing-loading">Loading…</section>;

  return (
    <section data-testid="briefing-view">
      <header className="page-head">
        <div>
          <h2>Briefing</h2>
          <p className="row-meta">Repo: {data.repo || repo}</p>
        </div>
      </header>
      {data.prs_err && (
        <p className="row-meta" role="alert">
          {data.prs_err}
        </p>
      )}
      <Section title="Needs attention" lines={data.attention} />
      {data.prs.length > 0 && (
        <section>
          <h3>Open pull requests</h3>
          <ul className="session-list">
            {data.prs.map((pr, i) => (
              // eslint-disable-next-line react/no-array-index-key
              <PrRow key={i} pr={pr} />
            ))}
          </ul>
        </section>
      )}
      <Section title="Reviews" lines={data.reviews_lines} />
      <Section title="Board" lines={data.board_lines} err={data.board_err} />
      <Section title="Conflicts" lines={data.conflicts_lines} />
      <Section title="Overlaps" lines={data.overlaps_lines} />
      <Section title="Failing runs" lines={data.workflow_lines} err={data.runs_err} />
    </section>
  );
}
