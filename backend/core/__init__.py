"""Shared backend routines (re-export surface; implementation lives in submodules).

Split from the former god file: env/config helpers in
:mod:`backend.core.env_config`, repo path helpers in
:mod:`backend.core.repos`, container health in
:mod:`backend.core.containers`, validators in
:mod:`backend.core.validation`, and git helpers in
:mod:`backend.core.git_helpers`. Prefer importing from those
modules directly; these re-exports stay for compatibility.
"""

from backend.core.containers import (
    CONTAINER_LOGS_TAIL_MAX,
    CONTAINER_SERVICE_RE,
    LOGS_TIMEOUT,
    RESTART_TIMEOUT,
    get_container_logs,
    get_container_stats,
    get_system_health,
    restart_container,
)
from backend.core.env_config import (
    FIXTURE_AUTO_DISCOVER_ENV,
    FIXTURE_BRIEFING_ENV,
    FIXTURE_FILENAME,
    briefing_fixture_candidate,
    fixture_auto_discover_enabled,
    get_env,
    load_config,
    resolve_briefing_fixture_path,
    run_td,
    to_json,
)
from backend.core.git_helpers import (
    CONSOLE_STASH_PREFIX,
    DIFF_CAP,
    DIFF_FILES_SHOWN,
    REPO_ACTION_TIMEOUT,
    STATUS_SHORT_LINES_CAP,
    TODO_HITS_SHOWN,
    get_repo_diff,
    get_repo_metrics,
    get_repo_status,
    list_repos,
    repo_discard_untracked,
    repo_stash,
    repo_sync,
    validate_action_branch,
)
from backend.core.repos import (
    REPO_FILES_CAP,
    REPO_FILES_SKIP_DIRS,
    get_checkout_path,
    get_repos_dir,
    list_repo_files,
    resolve_repo_path,
)
from backend.core.validation import (
    BRANCH_NAME_RE,
    REPO_NAME_RE,
    require_confirm,
    validate_repo_name,
)

__all__ = [
    "BRANCH_NAME_RE",
    "CONSOLE_STASH_PREFIX",
    "CONTAINER_LOGS_TAIL_MAX",
    "CONTAINER_SERVICE_RE",
    "DIFF_CAP",
    "DIFF_FILES_SHOWN",
    "FIXTURE_AUTO_DISCOVER_ENV",
    "FIXTURE_BRIEFING_ENV",
    "FIXTURE_FILENAME",
    "LOGS_TIMEOUT",
    "REPO_ACTION_TIMEOUT",
    "REPO_FILES_CAP",
    "REPO_FILES_SKIP_DIRS",
    "REPO_NAME_RE",
    "RESTART_TIMEOUT",
    "STATUS_SHORT_LINES_CAP",
    "TODO_HITS_SHOWN",
    "briefing_fixture_candidate",
    "fixture_auto_discover_enabled",
    "get_checkout_path",
    "get_container_logs",
    "get_container_stats",
    "get_env",
    "get_repo_diff",
    "get_repo_metrics",
    "get_repo_status",
    "get_repos_dir",
    "get_system_health",
    "list_repo_files",
    "list_repos",
    "load_config",
    "repo_discard_untracked",
    "repo_stash",
    "repo_sync",
    "require_confirm",
    "resolve_briefing_fixture_path",
    "resolve_repo_path",
    "restart_container",
    "run_td",
    "to_json",
    "validate_action_branch",
    "validate_repo_name",
]
