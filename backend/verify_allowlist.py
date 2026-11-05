"""Allowlist for repair verification commands (issue #483).

The repair worker executes the Verification Command parsed from an
issue body. Issue text is untrusted, so only known test/typecheck
verbs run: everything else is rejected before the worktree stage,
and runs are bounded by VERIFY_TIMEOUT. Trailing args pass through
to the tool (shell=False, worktree cwd), mirroring the exact-shape
anchoring of backend/exec/engine.py::validate_td_command.
"""

import shlex

# A hung verify must not hold the worker: the worker-level timeout
# (#435) bounds the whole run; this bounds each verification attempt.
VERIFY_TIMEOUT = 300

# Allowed leading argv shapes. pytest/unittest run the test suite,
# tsc/vitest the typecheck/frontend suite — the only commands a
# repair verification needs. npm/pnpm scripts are excluded: script
# indirection would smuggle unvetted commands past the verb check.
# npx single-name forms must match the REAL package npx resolves:
# `npx tsc` fetches the `tsc` squat placeholder, not TypeScript, so
# it stays rejected (plain `tsc` covers PATH-installed toolchains).
VERIFY_VERBS = (
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("python", "-m", "unittest"),
    ("python3", "-m", "unittest"),
    ("tsc",),
    ("vitest",),
    ("npx", "vitest"),
)

VERIFY_VERB_HELP = ("pytest, python[3] -m pytest/unittest, "
                    "tsc, vitest, npx vitest")


def validate_verify_command(text) -> list | None:
    """argv for an allowlisted verification command, or None.

    shlex.split, then argv[:len(verb)] must equal a known verb
    exactly. With shell=False downstream, metacharacters are inert
    literals, but non-verb commands are rejected regardless.
    """
    try:
        tokens = shlex.split(text or "")
    except Exception:
        return None
    for verb in VERIFY_VERBS:
        if tokens[:len(verb)] == list(verb):
            return tokens
    return None
