// Rollup guard (issue #610, #1386): per-repo views with no
// `repo=all` API; prompt for a repo instead of firing per-repo fetches
// at a pseudo-repo.
export function RollupGate({ view }: { view: string }) {
  return (
    <section data-testid="rollup-gate" role="status">
      <p className="row-meta">
        {view} needs one repo — pick one above to leave the All repos rollup.
      </p>
    </section>
  );
}
