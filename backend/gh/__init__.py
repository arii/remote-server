"""gh subprocess helper: run gh with a timeout and parse JSON.

Port of the gh call pattern in buttons/server.py (_td_run with gh
argv): commands are fixed argv lists built from validated values
only — never from free text. Unlike the legacy handler, which
shells out through the terminal container (EXEC + _td_prefix), this
backend invokes gh directly with an argv list (no shell), so no
quoting layer is needed at all.

``runner`` is an injectable subprocess-like module (for tests);
defaults to the real subprocess.
"""

from __future__ import annotations

import json
import re
import subprocess

GH_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

GH_TIMEOUT = 60


def validate_gh(gh: str) -> str:
    """owner/repo path for gh --repo; raises ValueError when invalid."""
    if not isinstance(gh, str) or not GH_RE.match(gh.strip()):
        raise ValueError(f"invalid gh repo path: {gh!r}")
    return gh.strip()


def repo_gh(name: str) -> str:
    """gh path for a validated repo basename (same convention as the
    briefing router: arii/<name>)."""
    from backend.core.validation import validate_repo_name

    return validate_gh(f"arii/{validate_repo_name(name)}")


def run_gh(gh: str, argv: list, timeout: int = GH_TIMEOUT,
           runner=None) -> str:
    """Run gh with an argv list; return stdout. Raises RuntimeError
    with the last stderr line on failure (same contract as _td_run).
    Raises ValueError when the argv does not scope a repo: every
    caller must pass ``--repo gh`` or a self-scoped ``repos/...``
    api URL, since the backend cwd is not a git repo (#544)."""
    validate_gh(gh)
    args = list(argv)
    if "--repo" not in args and not any(
            str(a).startswith("repos/") for a in args):
        raise ValueError(
            "gh argv must scope the repo (--repo or repos/... api URL)")
    run = runner or subprocess
    proc = run.run(
        ["gh"] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or "").strip().splitlines()[-1]
            if (proc.stderr or "").strip()
            else "gh command failed"
        )
    return proc.stdout


def run_gh_json(gh: str, argv: list, timeout: int = GH_TIMEOUT,
                runner=None):
    """run_gh + json.loads ({} / [] on empty stdout would be a gh
    контракт violation, so empty output decodes as an error)."""
    out = run_gh(gh, argv, timeout=timeout, runner=runner)
    if not out.strip():
        raise RuntimeError("gh returned empty output")
    return json.loads(out)
