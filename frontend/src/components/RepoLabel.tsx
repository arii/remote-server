// Shared view-level repo readout: one format everywhere a scrolled
// view names its repo (Chat + Briefing headers). `name · branch`
// when the branch is known, bare name otherwise.
export function repoLabelText(repo: string, branch?: string): string {
  return branch ? `${repo} · ${branch}` : repo;
}

export default function RepoLabel({ repo, branch }: { repo: string; branch?: string }) {
  return (
    <p className="row-meta" data-testid="repo-label">
      {repoLabelText(repo, branch)}
    </p>
  );
}
