"""Dispatcher: python3 -m core_cli <subcommand> [args].

Subcommand flags are each module's own (unchanged); this only routes.
Every module main takes argv (or ignores it) and returns an int exit
code or raises SystemExit with one.
"""

import sys

from . import ast_validator
from . import briefing_summary
from . import context_filter
from . import parse_task_spec
from . import preflight_digest
from . import repair_worker

COMMANDS = {
    "ast-check": ("Validate one file's syntax.", ast_validator.main),
    "briefing": ("Repo briefing / briefing plan.", briefing_summary.main),
    "context": ("Stdin context filters.", context_filter.main),
    "parse-task": ("Parse a task-spec file or stdin.", parse_task_spec.main),
    "preflight": ("Digest td preflight JSON on stdin.", preflight_digest.main),
    "repair": ("Autonomous repair worker.", repair_worker.main),
}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in COMMANDS:
        sys.stderr.write("usage: python3 -m core_cli <subcommand> [args]\n")
        for name, (blurb, _) in sorted(COMMANDS.items()):
            sys.stderr.write(f"  {name:11} {blurb}\n")
        return 2
    _, func = COMMANDS[args[0]]
    code = func(args[1:])
    return code if isinstance(code, int) else 0


if __name__ == "__main__":
    sys.exit(main())
