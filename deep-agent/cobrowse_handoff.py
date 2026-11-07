"""Agent-initiated co-browse handoff (#1097, adapted from
browser_research.pdf §Agent Wiring, cloud parts removed).

When the driving agent hits a login wall, MFA/2FA, CAPTCHA, payment
confirmation, or ambiguous UI, it calls ``request_human_cobrowse_handoff``
instead of stalling silently: the tool parks the agent on the shared
backend handoff contract (``POST /api/agent/handoff/request``) and
returns the interrupt-shaped payload the paper specifies
(``browser_handoff_requested`` + reason + live view URL). The human
acts in the Buttons co-browse view and hands back via
``POST /api/agent/handoff/resolve`` (the drawer's Hand Back button),
which resumes the agent with a mandatory DOM re-snapshot.

Local-first: no Stagehand/Steel/cloud bridge. The "live view" is our
CDP-direct viewer; an empty ``live_view_url`` means "open the Buttons
console co-browse view".
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable

try:
    from langchain_core.tools import tool
except ImportError:  # deep-agent venv absent (plain unit runs)
    def tool(fn=None, **kwargs):  # type: ignore[no-redef]
        if fn is None:
            return lambda f: f
        return fn

AGENT_API = os.environ.get("AGENT_API", "http://127.0.0.1:8000/api/agent")


def live_view_url_for(live_view_url: str = "") -> str:
    """Explicit URL wins; else derive from BUTTONS_PUBLIC_URL; else the
    Buttons console co-browse view (empty = no direct link)."""
    if (live_view_url or "").strip():
        return live_view_url.strip()
    public = os.environ.get("BUTTONS_PUBLIC_URL", "").strip().rstrip("/")
    return f"{public}/buttons/cobrowse" if public else ""


def handoff_payload(thread_id: str, reason: str, current_url: str = "",
                    live_view_url: str = "") -> dict[str, Any]:
    """Interrupt-shaped handoff payload (JSON-serializable)."""
    return {
        "event": "browser_handoff_requested",
        "thread_id": thread_id,
        "reason": reason,
        "current_url": current_url or "",
        "live_view_url": live_view_url_for(live_view_url),
        "instructions": (
            "Open the live view to complete the authentication/action, "
            "then hand back (the co-browse view's Hand Back button)."
        ),
    }


def _post_handoff_request(api_base: str, reason: str,
                           current_url: str) -> dict[str, Any]:
    body = json.dumps({
        "reason": reason,
        "current_url": current_url or None,
    }).encode("utf-8")
    req = urllib.request.Request(
        api_base.rstrip("/") + "/handoff/request", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def request_human_cobrowse_handoff_impl(
    thread_id: str,
    reason: str,
    current_url: str = "",
    live_view_url: str = "",
    post: Callable[..., dict[str, Any]] | None = None,
) -> str:
    """Park the browser agent on a human handoff and return the
    interrupt payload for the caller to raise.

    Call this IMMEDIATELY on a login wall, MFA/2FA prompt, CAPTCHA,
    payment confirmation, or ambiguous UI needing a human. Pauses
    agent driving (backend parks WAITING) and hands the live view to
    the user; the human's Hand Back resumes with a DOM re-snapshot.

    Args:
        thread_id: Co-browse session/thread owning the browser.
        reason: Why a human is needed (shown in the waiting banner).
        current_url: Page the agent is stuck on (may be blank).
        live_view_url: Direct live-view link; blank derives from
            BUTTONS_PUBLIC_URL or falls back to the console view.
        post: Injectable HTTP post (tests); defaults to the backend
            handoff endpoint.
    """
    if not (thread_id or "").strip():
        return "REJECTED: thread_id is required"
    if not (reason or "").strip():
        return "REJECTED: reason is required"
    payload = handoff_payload(thread_id.strip(), reason.strip(),
                              (current_url or "").strip(), live_view_url)
    sender = post or _post_handoff_request
    try:
        sender(AGENT_API, payload["reason"], payload["current_url"])
    except Exception as exc:
        return f"handoff unavailable: {exc}"
    return json.dumps(payload)


request_human_cobrowse_handoff = tool(request_human_cobrowse_handoff_impl)
