import { tdBlockReason } from './jobsUtils';
import type { PrRefState } from './jobsUtils';

export interface TdCommand {
  slug: string;
  label: string;
  desc: string;
  needsPr: boolean;
  build: (pr: string) => string;
}

const TD_COMMANDS: TdCommand[] = [
  // fix-ci was the first entry until #428 retired it: `td agent
  // fix-ci --execute` dispatches to a dead backend.
  {
    slug: 'read-reviews',
    label: 'Read PR reviews',
    desc: 'Review verdicts and threads for the PR.',
    needsPr: true,
    build: (pr) => `td gh read-pr-reviews --pr ${pr}`,
  },
  {
    slug: 'read-comments',
    label: 'Read PR comments',
    desc: 'All comments on the PR.',
    needsPr: true,
    build: (pr) => `td gh read-pr-comments --pr ${pr}`,
  },
  {
    slug: 'resolve-conflicts',
    label: 'Resolve conflicts',
    desc: 'Attempts merge-conflict resolution on the PR.',
    needsPr: true,
    build: (pr) => `td gh resolve-conflicts --pr ${pr}`,
  },
  {
    slug: 'doctor',
    label: 'Doctor',
    desc: 'Full-stack runtime diagnostic.',
    needsPr: false,
    build: () => 'td doctor',
  },
  {
    slug: 'preflight',
    label: 'Preflight',
    desc: 'Local checks before blaming CI.',
    needsPr: false,
    build: () => 'td preflight',
  },
  {
    slug: 'audit-pr',
    label: 'Audit PR',
    desc: 'Headless PR audit: context plus findings. Never submits.',
    needsPr: true,
    build: (pr) => `td gh audit-pr --pr ${pr} --fetch --audit`,
  },
  {
    slug: 'ai-review',
    label: 'AI review',
    desc: 'Quick heuristic review pass.',
    needsPr: true,
    build: (pr) => `td ai review ${pr}`,
  },
];

export default function TdCommands({
  pr,
  onPrChange,
  prStateCurrent,
  prOk,
  conflictBlock,
  onDispatch,
}: {
  pr: string;
  onPrChange: (value: string) => void;
  prStateCurrent: PrRefState | null;
  prOk: boolean;
  conflictBlock: string | null;
  onDispatch: (cmd: TdCommand) => void;
}) {
  return (
    <section aria-label="Agent commands">
      <h3>Agent commands</h3>
      <div className="cmd-group" data-testid="td-pr-group">
        <p className="row-meta">For a PR</p>
        <div className="actions">
          <label className="row-meta" htmlFor="td-pr">
            PR number{' '}
            <input
              id="td-pr"
              data-testid="td-pr"
              inputMode="numeric"
              value={pr}
              onChange={(e) => onPrChange(e.target.value)}
              placeholder="123"
            />
          </label>
        </div>
        {prStateCurrent && (
          <p className="row-meta" data-testid="td-pr-state">
            PR #{prStateCurrent.n}: CI {prStateCurrent.ci} ·{' '}
            {prStateCurrent.conflicted ? 'conflicted' : 'clean'}
          </p>
        )}
        <div className="actions">
          {TD_COMMANDS.filter((c) => c.needsPr).map((c) => (
            <button
              key={c.slug}
              type="button"
              data-testid={`dispatch-td-${c.slug}`}
              title={c.desc}
              disabled={!prOk || tdBlockReason(c.slug, prStateCurrent) !== null}
              onClick={() => onDispatch(c)}
            >
              {c.label}
            </button>
          ))}
        </div>
        {conflictBlock && (
          <p className="row-meta" data-testid="td-change-block">
            {conflictBlock}
          </p>
        )}
      </div>
      <div className="cmd-group" data-testid="td-repo-group">
        <p className="row-meta">Whole repo</p>
        <div className="actions">
          {TD_COMMANDS.filter((c) => !c.needsPr).map((c) => (
            <button
              key={c.slug}
              type="button"
              data-testid={`dispatch-td-${c.slug}`}
              title={c.desc}
              onClick={() => onDispatch(c)}
            >
              {c.label}
            </button>
          ))}
        </div>
      </div>
    </section>
  );
}
