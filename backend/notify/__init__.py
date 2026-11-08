"""Notify package: webhook auto-pull, prefs/state, digests, budget watch.

Split from the former god module into focused submodules; this file
holds imports + re-exports only (no def/class, no logic) so existing
`backend.notify.<name>` references keep resolving:

- backend.notify.webhook: HMAC verify + observe/pull/worker + state.
- backend.notify.prefs_state: notifier state, operator prefs, send gate.
- backend.notify.digest: digest text/schedule, notify check, delivery.
- backend.notify.budget: spend-cap edge alerts.
"""

from backend.notify.budget import BUDGET_BAND_SEVERITY
from backend.notify.budget import BUDGET_BLOCK_RATIO
from backend.notify.budget import BUDGET_WARN_RATIO
from backend.notify.budget import _budget_band
from backend.notify.budget import _budget_cap_spend
from backend.notify.budget import _budget_send_ok
from backend.notify.budget import _fire_budget_watch
from backend.notify.budget import _usd
from backend.notify.budget import fire_budget_watch
from backend.notify.digest import DEFAULT_DIGEST_SCHEDULE_PATH
from backend.notify.digest import DIGEST_SCHEDULE_INTERVALS
from backend.notify.digest import NOTIFY_GH_TIMEOUT
from backend.notify.digest import NOTIFY_PR_FIELDS
from backend.notify.digest import NOTIFY_RUN_FIELDS
from backend.notify.digest import _default_digest_schedule
from backend.notify.digest import _digest_failure_text
from backend.notify.digest import _digest_send_ok
from backend.notify.digest import _fire_due_digest
from backend.notify.digest import _read_registry_runs
from backend.notify.digest import build_digest_text
from backend.notify.digest import check_notifications_and_digest
from backend.notify.digest import collect_notify_brief
from backend.notify.digest import delivered
from backend.notify.digest import digest_interval_hours
from backend.notify.digest import digest_schedule_lock
from backend.notify.digest import digest_schedule_path
from backend.notify.digest import digest_schedule_status
from backend.notify.digest import fire_due_digest
from backend.notify.digest import load_digest_schedule
from backend.notify.digest import save_digest_schedule
from backend.notify.digest import set_digest_cadence
from backend.notify.prefs_state import MUTABLE_EVENTS
from backend.notify.prefs_state import NOTIFIER_STATE_PATH
from backend.notify.prefs_state import _UNSET
from backend.notify.prefs_state import console_base_url
from backend.notify.prefs_state import console_url
from backend.notify.prefs_state import enabled_events
from backend.notify.prefs_state import get_notification_prefs
from backend.notify.prefs_state import load_notifier_state
from backend.notify.prefs_state import notifier_lock
from backend.notify.prefs_state import save_notifier_state
from backend.notify.prefs_state import send_notification
from backend.notify.prefs_state import set_notification_prefs
from backend.notify.prefs_state import validate_muted_events
from backend.notify.webhook import WEBHOOK_FETCH_RETRIES
from backend.notify.webhook import WEBHOOK_FETCH_RETRY_DELAY
from backend.notify.webhook import WEBHOOK_MAX_BODY
from backend.notify.webhook import _REF_LOCK_RES
from backend.notify.webhook import _fetch_lock_contended
from backend.notify.webhook import _fetch_origin
from backend.notify.webhook import _git_base
from backend.notify.webhook import _heal_or_skip_dirty
from backend.notify.webhook import _skip_detail
from backend.notify.webhook import _webhook_target
from backend.notify.webhook import verify_signature
from backend.notify.webhook import webhook_observe
from backend.notify.webhook import webhook_pull
from backend.notify.webhook import webhook_pull_lock
from backend.notify.webhook import webhook_secret
from backend.notify.webhook import webhook_state
from backend.notify.webhook import webhook_state_lock
from backend.notify.webhook import webhook_pending
from backend.notify.webhook import webhook_worker

__all__ = [
    "BUDGET_BAND_SEVERITY",
    "BUDGET_BLOCK_RATIO",
    "BUDGET_WARN_RATIO",
    "DEFAULT_DIGEST_SCHEDULE_PATH",
    "DIGEST_SCHEDULE_INTERVALS",
    "MUTABLE_EVENTS",
    "NOTIFIER_STATE_PATH",
    "NOTIFY_GH_TIMEOUT",
    "NOTIFY_PR_FIELDS",
    "NOTIFY_RUN_FIELDS",
    "WEBHOOK_FETCH_RETRIES",
    "WEBHOOK_FETCH_RETRY_DELAY",
    "WEBHOOK_MAX_BODY",
    "_REF_LOCK_RES",
    "_UNSET",
    "_budget_band",
    "_budget_cap_spend",
    "_budget_send_ok",
    "_default_digest_schedule",
    "_digest_failure_text",
    "_digest_send_ok",
    "_fetch_lock_contended",
    "_fetch_origin",
    "_fire_budget_watch",
    "_fire_due_digest",
    "_git_base",
    "_heal_or_skip_dirty",
    "_read_registry_runs",
    "_skip_detail",
    "_usd",
    "_webhook_target",
    "build_digest_text",
    "check_notifications_and_digest",
    "collect_notify_brief",
    "console_base_url",
    "console_url",
    "delivered",
    "digest_interval_hours",
    "digest_schedule_lock",
    "digest_schedule_path",
    "digest_schedule_status",
    "enabled_events",
    "fire_budget_watch",
    "fire_due_digest",
    "get_notification_prefs",
    "load_digest_schedule",
    "load_notifier_state",
    "notifier_lock",
    "save_digest_schedule",
    "save_notifier_state",
    "send_notification",
    "set_digest_cadence",
    "set_notification_prefs",
    "validate_muted_events",
    "verify_signature",
    "webhook_observe",
    "webhook_pull",
    "webhook_pull_lock",
    "webhook_secret",
    "webhook_state",
    "webhook_state_lock",
    "webhook_pending",
    "webhook_worker",
]
