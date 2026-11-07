"""attention router: aggregated attention queue.

GET /api/attention?repo= returns {updated_at, items: [...]} where each
item is {id, kind, title, detail, repo, severity, action, created_at}.
Sources (existing helpers only, no new infra):
- failing runs: ops.fetch_workflow_health (workflow-health logic)
- failed jobs: jobs registry (done runs with a nonzero exit)
- failed webhooks: ledger kind-filtered reads
- skipped webhooks: newest unresolved pull skip per repo (info)
- pending approvals: ledger + jobs-registry pending records
- ollama outage: model_router.ollama_reachable probe (derived item —
  present while local models are down, clears itself on recovery;
  carries live metered spend/cap while failover is active)

Empty items = all-clear. All source readers are module-level
functions so tests can monkeypatch them (injectable seams).

Operators can snooze an item id for a preset duration (POST
/attention/snooze, POST /attention/unsnooze): snoozed ids are
filtered from the assembled list until expiry. The store is a small
JSON file mirroring the deferred.py pattern.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import lane_dispatch
from backend import ledger
from backend import webhook_heal
from backend.api.routers import agents
from backend.api.routers import ops
from backend.api.routers.jobs import registry
from backend.core import validate_repo_name

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

sys.path.insert(0, os.path.join(REPO_ROOT, "deep-agent"))

import model_router  # noqa: E402

router = APIRouter(prefix="/api", tags=["attention"])


class Action(BaseModel):
    label: str = ""
    method: str = "GET"
    href: str = ""
    destructive: bool = False
    intent: str = ""


class AttentionItem(BaseModel):
    id: str = ""
    kind: str = ""
    title: str = ""
    detail: str = ""
    repo: str = ""
    severity: str = "info"
    action: Action = Field(default_factory=Action)
    created_at: float = 0.0


class AttentionList(BaseModel):
    updated_at: float = 0.0
    items: list[AttentionItem] = Field(default_factory=list)


class SnoozeRequest(BaseModel):
    id: str = ""
    minutes: int = 0
    repo: str = "remote-server"


class SnoozeResult(BaseModel):
    ok: bool = True
    id: str = ""
    snoozed_until: float = 0.0


class UnsnoozeRequest(BaseModel):
    id: str = ""


class UnsnoozeResult(BaseModel):
    ok: bool = True
    id: str = ""
    snoozed: bool = False


DEFAULT_SNOOZE_PATH = "/var/lib/buttons/attention_snooze.json"

SNOOZE_PRESETS = (60, 480)

_snooze_lock = threading.Lock()


def snooze_path() -> str:
    """Current snooze file path (env override read dynamically so
    tests can point at a tmp dir without import tricks)."""
    return os.environ.get("BUTTONS_SNOOZE_PATH",
                          DEFAULT_SNOOZE_PATH)


def _row_until(entry) -> float:
    """Expiry of one snooze row; 0 for anything malformed so a bad
    row can never hide a card forever (issue #1030)."""
    if not isinstance(entry, dict):
        return 0.0
    until = entry.get("until", 0)
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return 0.0
    return float(until)


def _read_snooze() -> dict:
    """Whole store {item_id: {until, title, repo}}. {} on any failure
    (missing or corrupt file reads as empty; a write repairs it).
    Rows that are not dicts with a numeric until are dropped."""
    try:
        with open(snooze_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        if _row_until(v) <= 0:
            continue
        title = v.get("title", "")
        repo = v.get("repo", "")
        out[k] = {
            "until": _row_until(v),
            "title": title if isinstance(title, str) else "",
            "repo": repo if isinstance(repo, str) else "",
        }
    return out


def _write_snooze(store: dict) -> None:
    path = snooze_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, sort_keys=True)
    os.replace(tmp, path)


def _invalidate_inbox(name: str | None) -> None:
    """Drop cached inbox feeds after a snooze change (issue #1010).

    Now reads /api/inbox (SWR-cached), not /api/attention, so without
    this a snoozed card returns on refresh until the TTL lapses while
    the optimistic stub is already reconciled away. Lazy import: the
    inbox router imports this module at load time.
    """
    from backend.api.routers import inbox as inbox_mod
    inbox_mod.invalidate_inbox_cache(name)


def _snooze_filter(items: list, now: float) -> list:
    """Drop items snoozed past now; prune expired entries (persisted).

    Applies to assembled items, so it runs after the workflow-health
    TTL cache by construction — snooze never needs a cache bust.
    Entries for vanished ids are left alone (a flaky source must not
    eat a snooze); they self-clean at expiry.
    """
    with _snooze_lock:
        store = _read_snooze()
        live = {k: v for k, v in store.items()
                if _row_until(v) > now}
        if live != store:
            _write_snooze(live)
        return [i for i in items
                if _row_until(live.get(i.get("id", ""))) <= now]


# TTL cache: the gh subprocess chain behind workflow-health takes
# seconds; the Now queue polls it on every view. Freshness of a minute
# is plenty for an attention list. Tests reset _WF_CACHE directly.
WF_CACHE_TTL = 60.0
_WF_CACHE: dict[str, tuple[float, dict]] = {}


def get_workflow_health(repo: str) -> dict:
    """Injectable seam: failing-run diagnosis via workflow-health logic."""
    hit = _WF_CACHE.get(repo)
    if hit is not None and time.time() - hit[0] < WF_CACHE_TTL:
        return hit[1]
    data = ops.fetch_workflow_health(repo, f"arii/{repo}") or {}
    _WF_CACHE[repo] = (time.time(), data)
    return data


# TTL cache: the Now queue polls this endpoint on every view; a
# blackholed Ollama would otherwise stall each poll for the full
# probe timeout. Tests reset _OLLAMA_CACHE directly.
OLLAMA_CACHE_TTL = 60.0
_OLLAMA_CACHE: dict[str, tuple[float, bool]] = {}


def ollama_up() -> bool:
    """Injectable seam: local-model health for the outage item."""
    hit = _OLLAMA_CACHE.get("up")
    if hit is not None and time.time() - hit[0] < OLLAMA_CACHE_TTL:
        return hit[1]
    up = model_router.ollama_reachable(timeout=2)
    _OLLAMA_CACHE["up"] = (time.time(), up)
    return up


# Mirror of app.state.skills_verification_errors, published by the
# server lifespan (server.py) so the attention seam stays a plain
# module-level function tests can monkeypatch (issue #1134). The
# lifespan runs the verifier once at boot; re-verifying per poll
# would rehash every first-party skill on each Now view.
_skills_verification_errors: list = []


def publish_skills_verification_errors(errors: list) -> None:
    """Lifespan entry: record the boot-time verification outcome."""
    global _skills_verification_errors
    _skills_verification_errors = [str(e) for e in (errors or [])]


def list_skills_verification_errors() -> list:
    """Injectable seam: first-party skill pin mismatches (#1120)."""
    return list(_skills_verification_errors)


def failover_budget() -> dict:
    """Injectable seam: spend/cap for the metered-failover item.

    Reuses the agents budget endpoint shape (cap_usd, spent_usd,
    remaining_usd, blocked, unknown). A read failure degrades to
    zeros — the outage item still renders, never 500s.
    """
    try:
        return agents.agent_budget()
    except Exception as exc:
        logger.warning("attention: budget read failed: %s", exc)
        return {"cap_usd": None, "spent_usd": 0.0,
                "remaining_usd": None, "blocked": False,
                "unknown": 0}


def list_failed_jobs() -> list:
    """Injectable seam: failed runs from the jobs registry (#451).

    Runs are the one collection: a failed job is a done run with
    a nonzero exit.
    """
    out = []
    for job in registry.list_jobs():
        if not isinstance(job, dict):
            continue
        if job.get("done") is True and \
                job.get("exit_code") not in (None, 0):
            out.append(job)
    return out


def latest_webhook_success_ts() -> dict:
    """Injectable seam: newest advancing webhook pull ts per repo.

    Only a pull that moved the checkout (ok + advanced) counts as a
    success for resolution. Skips ("skipped: tree dirty",
    "skipped: tree not ff-able") and already-current no-ops record
    ok=True with advanced=False — the checkout did not move, so they
    must not resolve prior failures.
    """
    try:
        entries = ledger.read(kind="webhook", limit=200)
    except Exception:
        return {}
    best: dict = {}
    for e in entries:
        if isinstance(e, dict) and e.get("ok") is True \
                and e.get("advanced") is True:
            try:
                ts = float(e.get("ts", 0) or 0)
            except (TypeError, ValueError):
                continue
            repo = str(e.get("repo", ""))
            best[repo] = max(best.get(repo, 0.0), ts)
    return best


def list_failed_webhooks() -> list:
    """Injectable seam: failed webhooks from ledger kinds.

    Failures older than the latest advancing pull for the same repo
    are resolved history, not queue items — a later advancing pull
    means the checkout moved on. Cross-repo successes never resolve
    each other.
    """
    resolved_before = latest_webhook_success_ts()
    entries = ledger.read(kind="webhook", limit=50)
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            ts = float(e.get("ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= resolved_before.get(str(e.get("repo", "")), 0.0):
            continue
        blob = " ".join(str(e.get(k, "")) for k in
                        ("event", "status", "error", "detail")).lower()
        if "fail" in blob or "error" in blob or e.get("ok") is False \
                or e.get("status") in ("failed", "error"):
            out.append(e)
    return out


def list_skipped_webhooks() -> list:
    """Injectable seam: newest unresolved pull skip per repo.

    Skips (ok=True, advanced=False, detail "skipped: ...") are safe
    no-ops — the pull logic is unchanged — but a checkout that keeps
    skipping lags its remote default branch silently, so the newest
    skip per repo surfaces as its own info item until an advancing
    pull clears it.
    """
    resolved_before = latest_webhook_success_ts()
    entries = ledger.read(kind="webhook", limit=200)
    best: dict = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("ok") is not True or e.get("advanced") is True:
            continue
        detail = str(e.get("detail", "") or "")
        if not detail.strip().lower().startswith("skipped"):
            continue
        try:
            ts = float(e.get("ts", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        repo = str(e.get("repo", ""))
        if ts <= resolved_before.get(repo, 0.0):
            continue
        prev = best.get(repo)
        if prev is not None:
            try:
                prev_ts = float(prev.get("ts", 0) or 0)
            except (TypeError, ValueError):
                prev_ts = 0.0
            if ts < prev_ts:
                continue
        best[repo] = e
    return list(best.values())


def list_terminal_dispatches() -> list:
    """Injectable seam: terminal lane-dispatch items (done/failed).

    A terminal row surfaces until the operator removes it from the
    queue — completion is never silent, and clearing the row acks
    the item. Empty on any read failure (attention degrades).
    """
    try:
        rows = lane_dispatch.queue_list()
    except Exception as exc:
        logger.warning("attention: dispatch queue read failed: %s", exc)
        return []
    return [i for i in rows if isinstance(i, dict)
            and i.get("state") in ("done", "failed")]


def list_pending_approvals() -> list:
    """Injectable seam: pending approvals (ledger + jobs registry)."""
    out = []
    for e in ledger.read(kind="all", limit=100):
        if not isinstance(e, dict):
            continue
        if e.get("awaiting_approval") is True or e.get("pending_approval") is True \
                or str(e.get("event", "")).lower() in (
                    "approval requested", "approval pending", "awaiting approval"):
            out.append(e)
    try:
        for job in registry.list_jobs():
            if isinstance(job, dict) and job.get("awaiting_approval") is True:
                out.append(job)
    except Exception as exc:
        logger.warning("attention: jobs registry read failed: %s", exc)
    return out


def _event_repo(raw, name: str) -> str:
    """Short repo name for a global source's repo field.

    Ledger/jobs records carry `name` or `arii/name`; both normalize
    to the short checkout name for comparison. Missing/blank (legacy
    records with no repo) defaults to the queried repo so old items
    stay visible; unparseable values pass through verbatim and so
    filter out of every single-repo view.
    """
    text = str(raw or "").strip()
    if not text:
        return name
    try:
        return validate_repo_name(text)
    except ValueError:
        return text


def _run_created_ts(wf: dict, now: float) -> float:
    """Epoch stamp for a failing-run item: the run's own start time.

    fetch_workflow_health passes the run timestamps through (gh
    shape is an ISO string; fixtures may carry epoch numbers), so
    every poll stamps the same value instead of re-stamping now
    (#455). Missing or unparseable values fall back to poll time
    so the item still renders.
    """
    raw = (wf.get("created_at") or wf.get("createdAt")
           or wf.get("updated_at") or wf.get("updatedAt"))
    if isinstance(raw, bool):
        return now
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            try:
                parsed = datetime.fromisoformat(
                    text.replace("Z", "+00:00"))
            except ValueError:
                return now
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    return now


def _assemble_items(name: str, now: float) -> list[dict]:
    """Derived queue without snooze filtering (injectable-source
    assembly shared by the read path and the snooze 404 check).

    Single-repo contract (issue #723): the global sources (failed
    jobs, failed/skipped webhooks, pending approvals) filter to the
    queried repo — a single-repo feed carries only its own items.
    The rollup path fans out per repo, so it still sees every item
    exactly once.
    """
    items: list[dict] = []
    try:
        health = get_workflow_health(name) or {}
    except Exception as exc:
        logger.warning("attention: workflow-health read failed: %s", exc)
        health = {}
    seen_runs: dict[tuple[str, str], dict] = {}
    for wf in (health.get("workflows") or []):
        if not isinstance(wf, dict):
            continue
        rid = wf.get("id", "?")
        title = f"Failing workflow: {wf.get('name', 'Workflow')}"
        detail = wf.get("diagnosed_cause", "Workflow failed.")
        key = (title, detail)
        if key in seen_runs:
            seen_runs[key]["runs"] += 1
            continue
        item = {
            "id": f"run-{rid}",
            "kind": "failing-run",
            "title": title,
            "detail": detail,
            "repo": name,
            "severity": "crit",
            "action": {"label": "View health", "method": "GET",
                       "href": f"api/workflow-health?repo={name}",
                       "destructive": False, "intent": ""},
            "created_at": _run_created_ts(wf, now),
        }
        seen_runs[key] = {"item": item, "runs": 1}
        items.append(item)
    for entry in seen_runs.values():
        if entry["runs"] > 1:
            entry["item"]["title"] += f" ×{entry['runs']}"
    for job in list_failed_jobs():
        repo = _event_repo(job.get("repo"), name)
        if repo != name:
            continue
        jid = job.get("id", "?")
        items.append({
            "id": f"job-{jid}",
            "kind": "failed-job",
            "title": f"Failed job: {job.get('name', jid)}",
            "detail": str(job.get("error", job.get("detail", "job failed"))),
            "repo": repo,
            "severity": "warn",
            "action": {"label": "View jobs", "method": "GET",
                       "href": "api/jobs", "destructive": False, "intent": ""},
            "created_at": float(job.get("created_at", now) or now),
        })
    # One row per terminal dispatch (done info, failed warn): the
    # operator who is elsewhere still sees the outcome (#883).
    for item in list_terminal_dispatches():
        if item.get("state") not in ("done", "failed"):
            continue
        repo = _event_repo(item.get("repo"), name)
        if repo != name:
            continue
        failed = item.get("state") == "failed"
        label = f"{repo}#{item.get('issue', '?')}"
        title = str(item.get("title") or "").strip()
        if title:
            label += f" — {title}"
        detail = str(item.get("detail") or "").strip() \
            or ("repair worker failed" if failed
                else "repair worker finished")
        items.append({
            "id": f"dispatch-{repo}-{item.get('issue', '?')}",
            "kind": "failed-dispatch" if failed
            else "completed-dispatch",
            "title": f"Dispatch {'failed' if failed else 'done'}: "
                     f"{label}",
            "detail": detail,
            "repo": repo,
            "severity": "warn" if failed else "info",
            "action": {"label": "View queue", "method": "GET",
                       "href": "api/dispatch/queue",
                       "destructive": False, "intent": ""},
            "created_at": float(item.get("finished_at", now) or now),
        })
    # One row per incident: repeat failures with the same repo+detail
    # collapse into a single item with a count (latest first).
    webhook_incidents: dict = {}
    for e in list_failed_webhooks():
        repo = _event_repo(e.get("repo"), name)
        if repo != name:
            continue
        detail = str(e.get("error") or e.get("detail") or e.get("event")
                     or "webhook failed")
        key = (repo, detail)
        ts = float(e.get("ts", now) or now)
        prev = webhook_incidents.get(key)
        if prev is None or ts >= prev["created_at"]:
            eid = e.get("id", e.get("ts", "?"))
            webhook_incidents[key] = {
                "id": f"webhook-{eid}",
                "kind": "failed-webhook",
                "title": "Failed webhook delivery",
                "detail": detail,
                "repo": repo,
                "severity": "warn",
                "action": {"label": "View notifications", "method": "GET",
                           "href": "api/notifications/status",
                           "destructive": False, "intent": ""},
                "created_at": ts,
                "count": (prev["count"] + 1) if prev else 1,
            }
        else:
            prev["count"] += 1
    for item in webhook_incidents.values():
        if item["count"] > 1:
            item["title"] += f" (×{item['count']})"
        del item["count"]
        items.append(item)
    # One info row per lagging checkout (the seam already keeps only
    # the newest unresolved skip per repo, so this is collapsed by
    # construction). Skips never resolve failures — see
    # latest_webhook_success_ts.
    for e in list_skipped_webhooks():
        repo = _event_repo(e.get("repo"), name)
        if repo != name:
            continue
        detail = str(e.get("detail") or e.get("error") or e.get("event")
                     or "webhook pull skipped")
        # Inline cause (#855): the machine reason + ladder outcome
        # compose the precise recovery sentence after the raw skip
        # line, so the operator never has to decode git output.
        cause = webhook_heal.cause_for(e) if isinstance(e, dict) \
            else ""
        if cause:
            detail = f"{detail} — {cause}"[:300]
        eid = e.get("id", e.get("ts", "?"))
        items.append({
            "id": f"webhook-skip-{eid}",
            "kind": "skipped-webhook",
            "title": "Webhook pull skipped — checkout lagging",
            "detail": detail,
            "repo": repo,
            "severity": "info",
            "action": {"label": "Retry pull", "method": "POST",
                       "href": f"api/webhook/retry?repo={repo}",
                       "destructive": False, "intent": ""},
            "created_at": float(e.get("ts", now) or now),
        })
    for e in list_pending_approvals():
        repo = _event_repo(e.get("repo"), name)
        if repo != name:
            continue
        eid = e.get("id", e.get("ts", "?"))
        items.append({
            "id": f"approval-{eid}",
            "kind": "pending-approval",
            "title": str(e.get("title", "Approval pending")),
            "detail": str(e.get("detail", e.get("event", "awaiting approval"))),
            "repo": repo,
            "severity": "info",
            "action": {"label": "Review", "method": "GET",
                       "href": "api/prs?repo=" + name,
                       "destructive": False, "intent": ""},
            "created_at": float(e.get("ts", e.get("created_at", now)) or now),
        })
    # Tampered first-party skill pins (#1134): boot-time verifier
    # outcome, surfaced here because nobody watches the server log.
    # One collapsed item (stable snooze id); clean boot = no item.
    skill_problems = list_skills_verification_errors()
    if skill_problems:
        detail = "; ".join(skill_problems)[:500]
        items.append({
            "id": "skills-verification",
            "kind": "skills-verification",
            "title": "First-party skill verification failed — agent turns run UNVERIFIED",
            "detail": f"{detail} — fix the skill files or re-pin deep-agent/skills-lock.json (#1120).",
            "repo": name,
            "severity": "crit",
            "action": {},
            "created_at": now,
        })
    try:
        local_up = ollama_up()
    except Exception as exc:
        logger.warning("attention: ollama probe failed: %s", exc)
        local_up = True
    if not local_up:
        budget = failover_budget()
        try:
            spent = float(budget.get("spent_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            spent = 0.0
        cap = budget.get("cap_usd")
        cap_txt = "unlimited"
        if isinstance(cap, (int, float)) \
                and not isinstance(cap, bool):
            cap_txt = f"${cap:.2f}"
        items.append({
            "id": "ollama-outage",
            "kind": "provider-outage",
            "title": (f"Ollama unreachable \u2014 on metered Nebius "
                      f"(spend ${spent:.2f} / cap {cap_txt})"),
            "detail": ("Ollama is unreachable; default traffic falls back "
                       "to metered Nebius until it recovers. "
                       f"Lifetime metered spend ${spent:.4f} "
                       f"of {cap_txt}."),
            "repo": name,
            "severity": "warn",
            "action": {"label": "View budget", "method": "GET",
                       "href": "api/agents/budget",
                       "destructive": False, "intent": ""},
            "created_at": now,
        })
    return items


@router.get("/attention", response_model=AttentionList)
def attention(repo: str = "remote-server") -> dict:
    try:
        name = validate_repo_name(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    now = time.time()
    items = _snooze_filter(_assemble_items(name, now), now)
    return {"updated_at": now, "items": items}


@router.post("/attention/snooze", response_model=SnoozeResult)
def snooze_attention(body: SnoozeRequest) -> dict:
    if body.minutes not in SNOOZE_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"minutes must be one of {list(SNOOZE_PRESETS)}")
    try:
        name = validate_repo_name(body.repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    now = time.time()
    items = _assemble_items(name, now)
    match = next((i for i in items if i.get("id", "") == body.id), None)
    if match is None:
        raise HTTPException(
            status_code=404, detail=f"unknown attention id: {body.id}")
    until = now + body.minutes * 60
    with _snooze_lock:
        store = _read_snooze()
        # Title ships so a later session can render the Undo stub
        # (issue #1030); the 404 check above guarantees the row.
        store[body.id] = {
            "until": until,
            "title": str(match.get("title", "") or body.id),
            "repo": name,
        }
        _write_snooze(store)
    _invalidate_inbox(name)
    return {"ok": True, "id": body.id, "snoozed_until": until}


class SnoozedRow(BaseModel):
    id: str = ""
    title: str = ""
    until: float = 0.0
    repo: str = ""


class SnoozedList(BaseModel):
    snoozed: list[SnoozedRow] = Field(default_factory=list)


@router.get("/attention/snoozed", response_model=SnoozedList)
def list_snoozed() -> dict:
    """Live snoozed entries so a fresh session can render Undo stubs.

    Ids are global (no repo on the store), so no repo filter: entries
    carry their own repo. Expired rows prune on read, same as the
    attention filter (issue #1030).
    """
    now = time.time()
    with _snooze_lock:
        store = _read_snooze()
        live = {k: v for k, v in store.items()
                if _row_until(v) > now}
        if live != store:
            _write_snooze(live)
    rows = [
        {"id": k, "title": v.get("title", "") or k,
         "until": _row_until(v), "repo": v.get("repo", "")}
        for k, v in sorted(live.items(),
                           key=lambda kv: _row_until(kv[1]))
    ]
    return {"snoozed": rows}


@router.post("/attention/unsnooze", response_model=UnsnoozeResult)
def unsnooze_attention(body: UnsnoozeRequest) -> dict:
    with _snooze_lock:
        store = _read_snooze()
        store.pop(body.id, None)
        _write_snooze(store)
    # No repo on the request and snooze ids are global, so every
    # per-repo entry plus the rollup could serve the id: clear all.
    _invalidate_inbox(None)
    return {"ok": True, "id": body.id, "snoozed": False}
