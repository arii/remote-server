import { useState } from 'react';

// On-demand branch field for coding/mutating actions (#708):
// repair dispatch, jobs launches, and review merge/approve each
// carry this explicit field, defaulting to the repo's checkout
// branch. Repo-generic views stay repo-scoped (no branch shown).
export default function BranchField({
  id,
  testid,
  value,
  onChange,
}: {
  id: string;
  testid: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="row-meta" htmlFor={id}>
      Branch{' '}
      <input
        id={id}
        data-testid={testid}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="branch"
        autoComplete="off"
        spellCheck={false}
      />
    </label>
  );
}

// Branch state seeded from the repo default: the field opens on
// the checkout branch and re-seeds whenever the repo (or its
// default) changes, so a switch never sends a stale branch. The
// seed comparison runs during render (React's previous-render
// pattern), which also keeps server rendering honest.
export function useBranchField(
  repo: string,
  defaultBranch?: string,
): [string, (v: string) => void] {
  const seed = defaultBranch ?? '';
  const [branch, setBranch] = useState(seed);
  const [key, setKey] = useState(`${repo} ${seed}`);
  const now = `${repo} ${seed}`;
  if (now !== key) {
    setKey(now);
    setBranch(seed);
  }
  return [branch, setBranch];
}
