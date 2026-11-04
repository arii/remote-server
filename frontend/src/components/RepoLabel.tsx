// Shared view-level repo readout: one format everywhere a scrolled
// view names its repo (Chat + Briefing headers). Repo-only (#708):
// branch never scopes these views, so the label names just the repo.
export function repoLabelText(repo: string): string {
  return repo;
}

export default function RepoLabel({ repo }: { repo: string }) {
  return (
    <p className="row-meta" data-testid="repo-label">
      {repoLabelText(repo)}
    </p>
  );
}
