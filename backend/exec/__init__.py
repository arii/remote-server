"""Execution engine package: allowlisted command execution."""

from backend.exec.engine import (
    ALLOWED_ACTIONS,
    ALLOWED_PLANS,
    ALLOWED_TD_COMMANDS,
    MAX_OUTPUT,
    PLAN_TIMEOUT,
    TD_RUN_TIMEOUT,
    RunRegistry,
    append_output,
    build_plan_argv,
    td_run,
    truncate_tail,
    validate_td_command,
)

__all__ = [
    "ALLOWED_ACTIONS",
    "ALLOWED_PLANS",
    "ALLOWED_TD_COMMANDS",
    "MAX_OUTPUT",
    "PLAN_TIMEOUT",
    "TD_RUN_TIMEOUT",
    "RunRegistry",
    "append_output",
    "build_plan_argv",
    "td_run",
    "truncate_tail",
    "validate_td_command",
]
