"""Spend-cap budget watch (edge-fired warning/blocked alerts)."""

from __future__ import annotations

import time

from backend.notify.prefs_state import load_notifier_state
from backend.notify.prefs_state import notifier_lock
from backend.notify.prefs_state import save_notifier_state
from backend.notify.prefs_state import send_notification


# Budget watch (#652). Spend caps fail silently: GET
# /agents/budget shows cap/spent/blocked but nothing pushes when the
# cap is nearly hit or hit. This job rides the shared repair/digest
# tick in backend/api/routers/issues.py (a third job, not a ticker)
# and edge-fires one warning push at 80% and one blocked push at
# 100% of the metered cap per cap cycle.
BUDGET_WARN_RATIO = 0.8
BUDGET_BLOCK_RATIO = 1.0
BUDGET_BAND_SEVERITY = {"warning": 1, "blocked": 2}


def _budget_cap_spend():
    """(cap, spent) via the same modules as GET /agents/budget.

    Lazy import: runs.py pulls deep-agent-local modules (cli, ledger,
    pricing) that need the deep-agent dir on sys.path. Never raises:
    an unresolvable read reports uncapped (None) so a broken read
    stays silent instead of paging anyone.
    """
    try:
        import pricing
        import runs
        cap = pricing.spend_cap()
        spent = runs.total_metered_spend()["spent"]
        return cap, spent
    except Exception:
        return None, 0.0


def _budget_band(cap, spent):
    """Current alert band: None under 80%, "warning" at 80-100%,
    "blocked" at/over cap. cap <= 0 blocks everything, mirroring
    the spend-cap gate (0 blocks every metered call)."""
    if cap is None:
        return None
    if cap <= 0:
        return "blocked"
    ratio = max(0.0, spent) / cap
    if ratio >= BUDGET_BLOCK_RATIO:
        return "blocked"
    if ratio >= BUDGET_WARN_RATIO:
        return "warning"
    return None


def _budget_send_ok(results) -> bool:
    """True when at least one channel confirmed delivery. ANY-ok
    (digests demand all-ok): one confirmed push is enough for an
    edge alert — retrying every tick because a second channel is
    down would re-push the working one."""
    if not isinstance(results, dict) or not results:
        return False
    if "suppressed" in results:
        return False
    return any(isinstance(r, dict) and r.get("ok")
               for r in results.values())


def _usd(value: float) -> str:
    """$ with cents normally, micro precision for dust."""
    if abs(value) >= 0.01:
        return f"${value:,.2f}"
    return f"${value:,.6f}"


def fire_budget_watch(now: float | None = None, sender=None) -> bool:
    """Edge-fire budget alerts. Returns True when a send was
    attempted. Never raises: the shared tick must not die on a
    budget-watch failure."""
    try:
        return _fire_budget_watch(
            time.time() if now is None else now,
            sender or send_notification)
    except Exception:
        return False


def _fire_budget_watch(now: float, sender) -> bool:
    cap, spent = _budget_cap_spend()
    band = _budget_band(cap, spent)
    with notifier_lock:
        state = load_notifier_state()
        prev = (state.get("last_budget_alert") or {}).get("band")
    prev_sev = BUDGET_BAND_SEVERITY.get(prev, 0)
    if band is None:
        # Below every threshold: silently re-arm (drop the stamp so
        # the next crossing fires again, e.g. after a cap raise).
        if prev is not None:
            with notifier_lock:
                state = load_notifier_state()
                state.pop("last_budget_alert", None)
                save_notifier_state(state)
        return False
    if BUDGET_BAND_SEVERITY[band] <= prev_sev:
        # Same band: no repeat. Downward move (cap raised, spend
        # cleared): silently re-arm to the lower band so a later
        # re-crossing fires again. Drops never push — a "warning"
        # arriving right after "blocked" would be noise.
        if band != prev:
            with notifier_lock:
                state = load_notifier_state()
                state["last_budget_alert"] = {"band": band, "at": now}
                save_notifier_state(state)
        return False
    remaining = max(0.0, cap - spent)
    if band == "blocked":
        title = "Spend cap reached — metered calls blocked"
        message = (
            f"{_usd(spent)} of {_usd(cap)} cap spent. "
            "The next metered call will refuse until the cap is "
            "raised or spend is cleared.")
        priority = "high"
    else:
        title = "Spend cap 80% used"
        message = (
            f"{_usd(spent)} of {_usd(cap)} cap spent "
            f"({_usd(remaining)} remaining). "
            "Metered calls will be refused at 100%.")
        priority = "default"
    try:
        results = sender(title, message, priority=priority,
                         tags=["warning", "budget"],
                         event_type="budget")
    except Exception:
        # Failed attempt: no stamp, so the next tick retries (one
        # attempt per tick max). An edge alert is one-shot — losing
        # it on a transient failure would silence it forever.
        return True
    if isinstance(results, dict) and "suppressed" in results:
        # Suppressed (#532: suppressed sends never stamp): the next
        # tick retries, so the alert waits instead of being dropped.
        return True
    if _budget_send_ok(results):
        with notifier_lock:
            state = load_notifier_state()
            state["last_budget_alert"] = {"band": band, "at": now}
            save_notifier_state(state)
    return True
