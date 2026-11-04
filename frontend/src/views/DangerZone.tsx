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
  {
    action: 'recreate',
    label: 'Restart terminal',
    consequence: 'Force-restarts the terminal container; drops live sessions.',
    confirm: 'Live terminal sessions will drop.',
  },
  {
    action: 'recreate-code',
    label: 'Restart code editor',
    consequence: 'Force-restarts the code-server container.',
    confirm: 'The code editor will restart.',
  },
];

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
    <section aria-label="Danger zone" className="danger-zone">
      <h3>Danger zone</h3>
      <ul className="session-list">
        {DANGER.map((d) => {
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
                disabled={d.action === 'merge-approved' && mergeReady === 0}
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
              {runCount === 0 ? 'No runs to clear.' : 'Drops all runs, including running ones.'}
            </p>
          </div>
          <TapToFire
            label="Clear everything"
            confirm="Running runs will be killed."
            disabled={runCount === 0}
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
