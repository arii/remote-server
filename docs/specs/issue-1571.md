# Issue 1571 follow-up: trustworthy failure previews

## Existing outcome

PR 1700 already added failure classes, twenty-line trace previews, and recorded branch/PR links to the run summary. This follow-up repairs the current implementation. It does not apply the older saved model patch or change its benchmark result.

## Demonstrated current defects

On main 58a8155, exit_code "124" with a generic error was classified as agent_error. Text containing "pushup" became git_pr_error and "claimant" became claim_conflict. Trace previews returned credential-bearing URLs unchanged. The existing 38 run tests passed without covering these cases.

## Scope

- Keep the existing summary schema and five categories.
- Honor explicit categories, numeric/string timeout and temporary conflict exit codes, and boolean timeout/conflict flags.
- Match error words rather than unrelated substrings.
- Keep the twenty-line tail and realpath confinement to the trace directory.
- Sanitize structured credential keys, credential-labelled text, bearer values and credential-bearing URLs in the displayed tail. Preserve ordinary URL query parameters and useful failure text.
- Do not rewrite stored traces, change the browser skill-run collector, add model calls, or claim that arbitrary unlabelled secrets can be detected.

## Verification

- `python -m unittest discover -s tests -p 'test_run*.py'`: 44 tests passed, including six new regression methods with multiple cases.
- `python -m unittest discover -s backend/tests -p test_agent_runs_repo.py`: two tests passed.
- Ruff pyflakes checks, formatting and compile checks pass for changed Python files.
- Tests cover string/numeric codes, category precedence, unrelated-word false positives, twenty-line bounds, missing paths, symlink escape, credential sanitization without changing source bytes, and summary PR links.

## Deployment impact

Only the existing run-summary read path changes. No services are restarted. No trace files or ledgers are rewritten. This is not a complete trace-storage secret scrub: the existing detailed trace view and original trace files remain unchanged. No fresh paid benchmark was run; the saved benchmark remains a partial failure.
