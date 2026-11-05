import TapToFire from '../components/TapToFire';
import { mergeNoteFor } from './jobsUtils';

interface DangerAction {
  action: string;
  label: string;
  consequence: string;
  // Inline-confirm finality note; only irreversible / session-loss
  // actions carry one. The rest tap-fire directly.
  confirm?: string;
}

const DANGER: DangerAction[] = [
  {
    action: 'merge-approved',
    label: 'Merge approved PR',
    consequence: 'Merges every approved, green PR.',
    confirm: 'This cannot be undone.',
  },
  {
    action: 'create-plan',
    label: 'Write briefing plan',
    consequence: 'Writes a dated plan file into .agents/plans.',
  },
  {
    action: 'deploy-buttons',
    label: 'Redeploy this console',
    consequence: 'Pulls, rebuilds and restarts the console container.',
    confirm: 'The console will restart.',
  },
  // No terminal restart: force-recreating terminal drops live
  // agent sessions with no resume path, so the console must not
  // offer it (#444). recreate-code below is unaffected.
  {
    action: 'recreate-code',
    label: 'Restart code editor',
    consequence: 'Force-restarts the code-server container.',
    confirm: 'The code editor will restart.',
  },
];

// System operations rendered in the danger section (#934).
// `create-plan` is an agent workflow (writes a plan file, no service
// restart) and lives under Agent Workflows & Diagnostics instead —
// it stays in DANGER above so `dangerEntryFor` still resolves its
// re-run confirm metadata and schedule gating.
const SYSTEM_ACTIONS = new Set(['merge-approved', 'deploy-buttons', 'recreate-code']);

// Confirm metadata for destructive re-runs: the same challenge
// text fresh launches show, so a repeated tap replays identically.
// null = not a destructive action.
export function dangerEntryFor(action: string): { confirm?: string } | null {
  const found = DANGER.find((d) => d.action === action);
  return found ? { confirm: found.confirm } : null;
}

export default function DangerZone({
  mergeReady,
  finishedCount,
  runCount,
  onDangerAction,
  onClear,
  onTakeover,
}: {
  mergeReady: number | null;
  finishedCount: number;
  runCount: number;
  onDangerAction: (action: string, label: string) => void;
  onClear: (all: boolean) => void;
  onTakeover: () => void;
}) {
  return (
    <section aria-label="Danger / System Operations" className="danger-zone" data-testid="jobs-danger">
      <h3>Danger / System Operations</h3>
      <p className="row-meta">
        Disruptive — restarts services or disconnects viewers. Each action asks for confirmation first.
      </p>
      <ul className="session-list">
        {DANGER.filter((d) => SYSTEM_ACTIONS.has(d.action)).map((d) => {
          const mergeNote = mergeNoteFor(d.action, mergeReady);
          return (
            <li key={d.action} className="danger-row" data-testid={`danger-${d.action}`}>
              <div>
                <span className="row-title">{d.label}</span>
                <p className="row-meta">
                  {mergeNote === null ? (
                    d.consequence
                  ) : mergeReady === 0 ? (
                    mergeNote
                  ) : (
                    <>
                      {d.consequence} ({mergeNote})
                    </>
                  )}
                </p>
              </div>
              <TapToFire
                label={d.label}
                confirm={d.confirm}
                disabled={d.action === 'merge-approved' && (mergeReady === null || mergeReady === 0)}
                onFire={() => onDangerAction(d.action, d.label)}
              />
            </li>
          );
        })}
        <li className="danger-row" data-testid="danger-clear-finished">
          <div>
            <span className="row-title">Clear finished runs</span>
            <p className="row-meta">
              {finishedCount === 0
                ? 'Nothing finished to clear.'
                : 'Drops finished runs from the list below.'}
            </p>
          </div>
          <TapToFire
            label="Clear finished"
            disabled={finishedCount === 0}
            onFire={() => onClear(false)}
          />
        </li>
        <li className="danger-row" data-testid="danger-clear-all">
          <div>
            <span className="row-title">Clear everything</span>
            <p className="row-meta">
              {runCount === 0
                ? 'No runs to clear.'
                : finishedCount !== runCount
                  ? 'A run is still active — cancel it first.'
                  : 'Drops all runs.'}
            </p>
          </div>
          <TapToFire
            label="Clear everything"
            confirm="All runs will be dropped."
            disabled={runCount === 0 || finishedCount !== runCount}
            onFire={() => onClear(true)}
          />
        </li>
        <li className="danger-row" data-testid="danger-takeover">
          <div>
            <span className="row-title">Take over terminal</span>
            <p className="row-meta">Disconnects other viewers of the shared terminal.</p>
          </div>
          <TapToFire
            label="Take over terminal"
            confirm="Other viewers will be disconnected."
            onFire={onTakeover}
          />
        </li>
      </ul>
    </section>
  );
}
