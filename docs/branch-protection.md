# Branch Protection on `main`

## Context & Problem
Issue: Main branch unprotected — CI gates (#498, #509) were advisory because no required status checks were configured on `main`. Without branch protection, broken builds or failing `validate-config` steps could be merged freely without blocking on red CI.

## Required Status Checks
Branch protection on `main` requires passing status checks from `.github/workflows/ci.yml` before merging pull requests.

The required status check contexts from CI:
- `backend` (backend tests + type-check + validate-config)
- `frontend` (frontend tsc + vitest)
- `shell` (lint shell scripts)
- `compose` (validate compose file)
- `secret-scan` (scan for leaked secrets)
- `audit` (fallow code audit)

## Configuration via GitHub REST API

Branch protection can be applied using the GitHub REST API endpoint `PUT /repos/arii/remote-server/branches/main/protection` with the following configuration:

```bash
gh api --method PUT repos/arii/remote-server/branches/main/protection \
  --input - <<'EOF'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "backend",
      "frontend",
      "shell",
      "compose",
      "secret-scan",
      "audit"
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
EOF
```

Or using curl:
```bash
curl -X PUT \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer <GITHUB_TOKEN>" \
  https://api.github.com/repos/arii/remote-server/branches/main/protection \
  -d '{
    "required_status_checks": {
      "strict": true,
      "contexts": [
        "backend",
        "frontend",
        "shell",
        "compose",
        "secret-scan",
        "audit"
      ]
    },
    "enforce_admins": false,
    "required_pull_request_reviews": null,
    "restrictions": null
  }'
```

## Lane Orchestrator & Automation Compatibility
The lane orchestrator (`.agents/skills/lane-orchestrator/SKILL.md`) already mandates:
> "Merge green, pre-approved PRs whose checks pass."
> "Never merge a PR that is not green and pre-approved."

With branch protection enabled, GitHub enforces merge-on-green at the repository level, guaranteeing that PRs with failing tests or unvalidated configs cannot be merged into `main`.
