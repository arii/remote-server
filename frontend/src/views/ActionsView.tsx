import { useState } from 'react';

// Grouped session actions per #204 direction. Plain buttons; no navigation.
const GROUPS: Array<{ label: string; actions: string[] }> = [
  { label: 'Session', actions: ['Clear selection'] },
  { label: 'Jobs', actions: ['Run job', 'Clear finished'] },
  { label: 'System', actions: ['Refresh health'] },
];

export default function ActionsView({
  onAction,
}: {
  onAction: (action: string) => void;
}) {
  const [last, setLast] = useState<string | null>(null);
  return (
    <section data-testid="actions-view">
      {GROUPS.map((g) => (
        <div key={g.label}>
          <h3>{g.label}</h3>
          <div className="actions" role="group" aria-label={g.label}>
            {g.actions.map((a) => (
              <button
                key={a}
                type="button"
                onClick={() => {
                  setLast(a);
                  onAction(a);
                }}
              >
                {a}
              </button>
            ))}
          </div>
        </div>
      ))}
      {last && (
        <p className="row-meta" data-testid="actions-last">
          Last action: {last}
        </p>
      )}
    </section>
  );
}
