"""briefing + ledger router: ports of the buttons endpoints.

Same routes/verbs as buttons/server.py, under the backend's /api
prefix convention (buttons serves /buttons/api/*; this backend serves
/api/* like its sibling routers):

- GET /api/briefing?repo=  structured briefing passthrough
- POST /api/briefing/refresh  background regenerate (deduped)
- GET /api/status           backend status (health + empty run lists)
- GET /api/ledger?kind=&limit=&since=  newest-first audit entries
- POST /api/ledger/clear    truncate + tombstone (gated: intent
  "ledger-clear" + confirmed, same as jobs/clear)

Briefing reads are NOT ledgered: reads are not actions.
"""

import json
import logging
import os
import subprocess
import threading
import time
import urllib.request

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.core import (
    briefing_fixture_candidate,
    get_system_health,
    resolve_briefing_fixture_path,
    validate_repo_name,
)
from backend.exec.engine import stack_dir
from backend.ledger import LEDGER_KINDS, clear as ledger_clear
from backend.ledger import read as ledger_read

router = APIRouter(prefix="/api", tags=["briefing"])

logger = logging.getLogger(__name__)


# Narrative lede (issue #671): a short AI-written prose paragraph
# (top risk + top action) generated during background refresh and
# cached with the SWR entry. Reads never block on it — stale prose
# serves instantly while fresh prose regenerates — and any narrator
# failure degrades to data-without-prose (today's briefing).
#
# Cutoff story: local-first. BRIEFING_NARRATOR takes
# "ollama:<model>" (default when unset: ASK_MODEL or qwen3:8b) or
# "nebius:<model>" as an explicit metered opt-in; "off" disables.
# Unknown values disable. Raw urllib only — no langchain in backend/.
# Env names/defaults mirror deep-agent/model_router without importing it.
NARRATOR_ENV = "BRIEFING_NARRATOR"
# Issue #721: 90s never fit stock CPU/Ollama latency — a tiny probe
# took 81.7s and the real 6000-char prompt always exceeded 90s, so
# narrative stayed null forever. 300s covers observed latency.
NARRATOR_TIMEOUT = 300.0
NARRATOR_MAX_INPUT_CHARS = 6000
NARRATOR_MAX_OUTPUT_CHARS = 2000
NARRATOR_MAX_TOKENS = 200
NARRATOR_SYSTEM = (
    "Write a 2-4 sentence briefing lede: name the top risk, then the "
    "single most important action. Plain sentences only — no headers, "
    "no bullets, no fluff."
)
# Issue #802: compact/fast variant for the cold-fetch path. Stock
# Ollama is decode-bound (~6 tok/s on qwen3:8b), so the win is
# output tokens, not input chars: key facts only (1500-char budget)
# plus a rigid one-line "Risk: …; Fix: …" shape the model completes
# in ~13 tokens (p50 2.0s vs 6.1s full on stock qwen3:8b, measured).
# The 32-token cap is
# headroom; length-truncated tails are clause-cut or dropped, never
# shipped corrupt. Background refresh always regenerates full prose,
# upgrading compact entries.
NARRATOR_COMPACT_MAX_INPUT_CHARS = 1500
NARRATOR_COMPACT_MAX_TOKENS = 32
NARRATOR_COMPACT_SYSTEM = (
    "Reply with ONE line, max 8 words, exact shape "
    "'Risk: <risk>; Fix: <fix>'. Fragments ok, no preamble."
)
NARRATOR_MODE_FULL = "full"
NARRATOR_MODE_COMPACT = "compact"
# Compact fact pick: (key, max items). Attention first — it already
# ranks what matters — then the failure/conflict lines, then errors.
NARRATOR_COMPACT_FACTS = (
    ("attention", 5),
    ("failing_runs", 3),
    ("conflicts_lines", 3),
    ("reviews_lines", 3),
    ("workflow_lines", 3),
)
NARRATOR_OFF_VALUES = frozenset(
    {"off", "none", "disabled", "0", "false", "no"})
NARRATOR_SKIP_KEYS = frozenset(
    {"narrative", "narrative_at", "narrative_mode", "cached", "stale",
     "generated_at"})
DEFAULT_NARRATOR_OLLAMA_URL = "http://ollama:11434"
DEFAULT_NARRATOR_OLLAMA_MODEL = "qwen3:8b"
DEFAULT_NARRATOR_NEBIUS_URL = "https://api.tokenfactory.nebius.com/v1"
DEFAULT_NARRATOR_NEBIUS_MODEL = "zai-org/GLM-5.3-Flash"


class Briefing(BaseModel):
    """Structured briefing payload: shape of analyze() in
    core_cli/briefing_summary.py. Extra keys allowed so new briefing
    sections pass through instead of 500ing."""

    model_config = ConfigDict(extra="allow")

    repo: str = ""
    attention: list = Field(default_factory=list)
    attention_with_markers: list = Field(default_factory=list)
    prs: list = Field(default_factory=list)
    prs_err: str = ""
    enrichment_note: str = ""
    enrichment_total: int = 0
    enrichment_shown: int = 0
    enrichment_truncated: bool = False
    reviews_lines: list = Field(default_factory=list)
    reviews_err: dict = Field(default_factory=dict)
    board: list = Field(default_factory=list)
    board_lines: list = Field(default_factory=list)
    board_err: str = ""
    conflicts: list = Field(default_factory=list)
    conflicts_lines: list = Field(default_factory=list)
    conf_err: str = ""
    overlaps: list = Field(default_factory=list)
    overlaps_lines: list = Field(default_factory=list)
    failing_runs: list = Field(default_factory=list)
    workflow_lines: list = Field(default_factory=list)
    runs_err: str = ""
    narrative: str | None = None
    narrative_at: float | None = None
    # Issue #802: which prompt variant produced the prose — "compact"
    # (fast cold-fetch lede) or "full" (background refresh upgrade).
    # Empty when no prose is attached.
    narrative_mode: str = ""
    # Fixture marker (#453): True + source path when canned data
    # serves instead of the live subprocess, so operators can tell
    # fixture from live. Live responses carry the False default.
    fixture: bool = False
    fixture_source: str = ""


class BackendStatus(BaseModel):
    """Backend status: mirrors the shape of /buttons/api/status
    (runs, webhooks, health) but honest — this backend owns no run
    queue, so runs/webhooks are empty and no activity is faked."""

    model_config = ConfigDict(extra="allow")

    ok: bool = True
    service: str = "backend"
    runs: list = Field(default_factory=list)
    webhooks: dict = Field(default_factory=dict)
    health: dict = Field(default_factory=dict)


class LedgerList(BaseModel):
    entries: list = Field(default_factory=list)


class LedgerClearRequest(BaseModel):
    intent: str = ""
    confirmed: bool = False


class LedgerClearResult(BaseModel):
    ok: bool = True


class BriefingRefreshRequest(BaseModel):
    repo: str = "remote-server"


class BriefingRefreshResult(BaseModel):
    ok: bool = True
    repo: str = ""
    started: bool = False


# TTL cache with stale-while-revalidate: the gh fan-out behind
# briefing_summary takes ~6s and every uncached visit blocks on it.
# Fresh entries serve instantly; stale entries serve instantly and
# refresh on a daemon thread; entries past MAX_STALE recompute
# synchronously so a broken refresher can never pin ancient data.
# Tests reset _BRIEFING_CACHE/_BRIEFING_REFRESHING directly.
BRIEFING_CACHE_TTL = 60.0
BRIEFING_CACHE_MAX_STALE = 1800.0
_BRIEFING_CACHE: dict[str, tuple[float, dict]] = {}
_BRIEFING_REFRESHING: set[str] = set()
_BRIEFING_LOCK = threading.Lock()


def _store_briefing(repo: str, data: dict) -> float:
    ts = time.time()
    with _BRIEFING_LOCK:
        _BRIEFING_CACHE[repo] = (ts, data)
        _BRIEFING_REFRESHING.discard(repo)
    return ts


def _narrator_env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    if not isinstance(val, str) or not val.strip():
        return default
    return val


def narrator_spec() -> dict | None:
    """Parse BRIEFING_NARRATOR into {provider, model, base_url}.

    None means narration disabled: an explicit off value, or
    anything unrecognized (fail safe — never metered, never raises).
    """
    raw = _narrator_env(NARRATOR_ENV)
    if not raw:
        return {
            "provider": "ollama",
            "model": _narrator_env(
                "ASK_MODEL", DEFAULT_NARRATOR_OLLAMA_MODEL),
            "base_url": _narrator_env(
                "OLLAMA_URL", DEFAULT_NARRATOR_OLLAMA_URL),
        }
    text = raw.strip()
    low = text.lower()
    if low in NARRATOR_OFF_VALUES:
        return None
    if low == "ollama" or low.startswith("ollama:"):
        model = text.split(":", 1)[1].strip() if ":" in text else ""
        return {
            "provider": "ollama",
            "model": model or _narrator_env(
                "ASK_MODEL", DEFAULT_NARRATOR_OLLAMA_MODEL),
            "base_url": _narrator_env(
                "OLLAMA_URL", DEFAULT_NARRATOR_OLLAMA_URL),
        }
    if low == "nebius" or low.startswith("nebius:"):
        model = text.split(":", 1)[1].strip() if ":" in text else ""
        return {
            "provider": "nebius",
            "model": model or _narrator_env(
                "NEBIUS_MODEL", DEFAULT_NARRATOR_NEBIUS_MODEL),
            "base_url": _narrator_env(
                "NEBIUS_BASE_URL", DEFAULT_NARRATOR_NEBIUS_URL),
        }
    logger.warning("briefing: ignoring unknown %s value %r",
                   NARRATOR_ENV, raw)
    return None


def narrator_compact_facts(data: dict) -> dict:
    """Key facts only: repo + top attention/failure lines + PR peek.

    Defensive about shapes — briefing sections are lists of strings
    or dicts today, but a scalar or missing key must never break the
    fast path; it just contributes less.
    """
    facts: dict = {}
    if data.get("repo"):
        facts["repo"] = data["repo"]
    for key, limit in NARRATOR_COMPACT_FACTS:
        vals = data.get(key)
        if isinstance(vals, (list, tuple)):
            vals = list(vals)[:limit]
            if vals:
                facts[key] = vals
        elif vals:
            facts[key] = vals
    prs = data.get("prs")
    if isinstance(prs, (list, tuple)) and prs:
        facts["prs_open"] = len(prs)
        peek = []
        for pr in list(prs)[:3]:
            if isinstance(pr, dict):
                peek.append({k: pr[k] for k in ("number", "title")
                             if k in pr})
            else:
                peek.append(str(pr))
        facts["prs"] = peek
    for key in ("prs_err", "runs_err", "conf_err", "board_err"):
        if data.get(key):
            facts[key] = data[key]
    return facts


def narrator_prompt(data: dict, compact: bool = False) -> str:
    """User prompt: condensed briefing JSON.

    Freshness markers and previous prose are excluded (no feedback
    loop); oversize briefings truncate to the input budget. Compact
    mode carries key facts only under the smaller fast-path budget.
    """
    if compact:
        slim = narrator_compact_facts(data)
        budget = NARRATOR_COMPACT_MAX_INPUT_CHARS
    else:
        slim = {k: v for k, v in data.items()
                if k not in NARRATOR_SKIP_KEYS}
        budget = NARRATOR_MAX_INPUT_CHARS
    blob = json.dumps(slim, sort_keys=True, default=str)
    if len(blob) > budget:
        blob = blob[:budget] + "…[truncated]"
    return f"Briefing JSON:\n{blob}\n\nLede:"


def _post_narrator(url: str, payload: dict, headers: dict,
                   timeout: float) -> dict:
    """Injectable seam: one raw-HTTP LLM call, like repair's client."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ollama_base(base_url: str) -> str:
    base = ((base_url or "").strip().rstrip("/")
            or DEFAULT_NARRATOR_OLLAMA_URL)
    if base.lower().endswith("/api/generate"):
        base = base[: -len("/api/generate")]
    return base


def _strip_thinking(text: str) -> str:
    """Drop <think> traces reasoning models (qwen3) prepend.

    The payload already asks for think=false, but older servers
    ignore that flag — never let chain-of-thought leak into the lede.
    """
    while True:
        start = text.find("<think>")
        if start < 0:
            break
        end = text.find("</think>", start)
        if end < 0:
            text = text[:start]
            break
        text = text[:start] + text[end + len("</think>"):]
    return text


def _finish_compact(text: str) -> str:
    """Salvage a length-truncated compact lede: keep through the last
    complete clause so a cut-off tail never ships corrupt prose (a
    half-written PR number, a dangling word). No complete clause —
    no prose; the caller degrades to data-without-prose as usual."""
    stripped = text.strip()
    if not stripped:
        return ""
    if stripped[-1] in ".!?":
        return stripped
    cut = max(stripped.rfind("."), stripped.rfind("!"),
              stripped.rfind("?"), stripped.rfind(";"))
    if cut < 0:
        return ""
    head = stripped[:cut + 1]
    if head[-1] == ";":
        head = head[:-1].rstrip()
    return head


def _narrate_ollama(spec: dict, prompt: str, timeout: float,
                    system: str,
                    max_tokens: int) -> tuple[str, bool]:
    """Ollama call; second return flags token-cap truncation."""
    body = _post_narrator(
        _ollama_base(spec["base_url"]) + "/api/generate",
        # think=false: reasoning traces burn the token budget and
        # minutes of CPU on a 2-sentence lede (measured 31s/10tok
        # with thinking vs 1.2s without on qwen3:8b). Servers that
        # predate the flag ignore it; _strip_thinking covers those.
        {"model": spec["model"], "system": system,
         "prompt": prompt, "stream": False, "think": False,
         "options": {"temperature": 0.2,
                     "num_predict": max_tokens}},
        {"Content-Type": "application/json"}, timeout)
    return body.get("response", ""), body.get("done_reason") == "length"


def _narrate_nebius(spec: dict, prompt: str, timeout: float,
                    system: str,
                    max_tokens: int) -> tuple[str, bool]:
    """Nebius call; second return flags token-cap truncation."""
    key = _narrator_env("NEBIUS_API_KEY")
    if not key:
        logger.warning(
            "briefing: nebius narrator opted in without NEBIUS_API_KEY")
        return "", False
    body = _post_narrator(
        (spec["base_url"] or "").strip().rstrip("/")
        + "/chat/completions",
        {"model": spec["model"],
         "messages": [
             {"role": "system", "content": system},
             {"role": "user", "content": prompt}],
         "temperature": 0.2, "max_tokens": max_tokens},
        {"Content-Type": "application/json",
         "Authorization": f"Bearer {key}"},
        timeout)
    choices = body.get("choices") or []
    if not choices:
        return "", False
    first = choices[0] or {}
    return ((first.get("message") or {}).get("content", ""),
            first.get("finish_reason") == "length")


def narrate_briefing(data: dict,
                     timeout: float = NARRATOR_TIMEOUT,
                     compact: bool = False) -> str | None:
    """Generate the lede, or None on any failure.

    Never raises and never makes a metered call unless the nebius:
    opt-in is set — the default path only talks to local Ollama.
    Compact mode uses the key-facts prompt, the one-line Risk/Fix
    system shape, and the smaller output cap (cold-fetch fast path);
    a length-truncated compact tail is clause-cut or dropped.
    """
    spec = narrator_spec()
    if spec is None:
        return None
    prompt = narrator_prompt(data, compact=compact)
    if compact:
        system, max_tokens = (
            NARRATOR_COMPACT_SYSTEM, NARRATOR_COMPACT_MAX_TOKENS)
    else:
        system, max_tokens = NARRATOR_SYSTEM, NARRATOR_MAX_TOKENS
    start = time.monotonic()
    try:
        if spec["provider"] == "ollama":
            text, truncated = _narrate_ollama(
                spec, prompt, timeout, system, max_tokens)
        elif spec["provider"] == "nebius":
            text, truncated = _narrate_nebius(
                spec, prompt, timeout, system, max_tokens)
        else:  # pragma: no cover - narrator_spec only yields these two
            return None
    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.warning(
            "briefing: narrator failed after %.1fs (%s)", elapsed, exc)
        return None
    text = _strip_thinking(text or "").strip()
    if compact and truncated:
        text = _finish_compact(text)
    if not text:
        return None
    if len(text) > NARRATOR_MAX_OUTPUT_CHARS:
        text = text[:NARRATOR_MAX_OUTPUT_CHARS].rstrip() + "…"
    return text


def _attach_narrative(data: dict, previous: dict | None) -> dict:
    """Fresh FULL prose onto data; narrator failure carries previous
    prose or omits the block — data is never blocked and never errors.

    Background refresh always regenerates full prose, which is the
    upgrade path for compact cold-fetch ledes (#802).
    """
    prose = narrate_briefing(data)
    if prose:
        data["narrative"] = prose
        data["narrative_at"] = time.time()
        data["narrative_mode"] = NARRATOR_MODE_FULL
    elif previous and previous.get("narrative"):
        data["narrative"] = previous["narrative"]
        data["narrative_at"] = previous.get("narrative_at")
        if previous.get("narrative_mode"):
            data["narrative_mode"] = previous["narrative_mode"]
        else:
            data.pop("narrative_mode", None)
    else:
        data.pop("narrative", None)
        data.pop("narrative_at", None)
        data.pop("narrative_mode", None)
    return data


def _patch_narrative(repo: str, ts: float, snapshot: dict) -> None:
    """Fill COMPACT prose into a cold-cache entry behind the instant read.

    Only patches when the entry is still this generation — a newer
    refresh must never be clobbered with older prose. The compact
    lede upgrades to full on the next background refresh (#802).
    """
    prose = narrate_briefing(snapshot, compact=True)
    if not prose:
        return
    with _BRIEFING_LOCK:
        hit = _BRIEFING_CACHE.get(repo)
        if hit is None or hit[0] != ts:
            return
        hit[1]["narrative"] = prose
        hit[1]["narrative_at"] = time.time()
        hit[1]["narrative_mode"] = NARRATOR_MODE_COMPACT


def _spawn_narrative_patch(repo: str, ts: float, snapshot: dict) -> None:
    if narrator_spec() is None:
        return
    thread = threading.Thread(
        target=_patch_narrative, args=(repo, ts, snapshot), daemon=True)
    thread.start()


def _refresh_briefing(repo: str, gh: str) -> None:
    """Background SWR refresh: failures keep the stale entry."""
    try:
        data = _run_briefing(repo, gh)
    except Exception:
        with _BRIEFING_LOCK:
            _BRIEFING_REFRESHING.discard(repo)
        return
    with _BRIEFING_LOCK:
        prev = _BRIEFING_CACHE.get(repo)
    _store_briefing(
        repo, _attach_narrative(data, prev[1] if prev else None))


def _revalidate_briefing(repo: str, gh: str) -> None:
    with _BRIEFING_LOCK:
        if repo in _BRIEFING_REFRESHING:
            return
        _BRIEFING_REFRESHING.add(repo)
    thread = threading.Thread(
        target=_refresh_briefing, args=(repo, gh), daemon=True)
    thread.start()


def get_briefing(name: str, gh: str) -> dict:
    """Injectable seam: briefing payload with SWR caching.

    Every response carries freshness markers (cached/stale/
    generated_at) that the Briefing model passes through via
    extra="allow" so the view can show a refresh indicator.
    Copies annotate per-request markers; the cached entry itself
    is never mutated.
    """
    now = time.time()
    hit = _BRIEFING_CACHE.get(name)
    if hit is not None:
        age = now - hit[0]
        if age < BRIEFING_CACHE_TTL:
            return {**hit[1], "cached": True, "stale": False,
                    "generated_at": hit[0]}
        if age < BRIEFING_CACHE_MAX_STALE:
            _revalidate_briefing(name, gh)
            return {**hit[1], "cached": True, "stale": True,
                    "generated_at": hit[0]}
    previous = hit[1] if hit is not None else None
    data = _run_briefing(name, gh)
    if previous and previous.get("narrative") \
            and "narrative" not in data:
        data["narrative"] = previous["narrative"]
        data["narrative_at"] = previous.get("narrative_at")
        if previous.get("narrative_mode"):
            data["narrative_mode"] = previous["narrative_mode"]
    ts = _store_briefing(name, data)
    _spawn_narrative_patch(name, ts, dict(data))
    return {**data, "cached": False, "stale": False,
            "generated_at": ts}


def warm_briefing_cache(repos: list[str] | None = None) -> list[str]:
    """Best-effort boot precache (#929): generate snapshots now.

    Sequential get_briefing per repo (fresh cache hits return
    instantly); per-repo failures are skipped, never raised.
    Returns the warmed repo names. The server lifespan runs this
    on a daemon thread so boot never blocks on generation.
    """
    warmed: list[str] = []
    for repo in repos if repos is not None else ["remote-server"]:
        try:
            name = validate_repo_name(repo)
        except ValueError:
            continue
        try:
            get_briefing(name, f"arii/{name}")
        except Exception:
            continue
        warmed.append(name)
    return warmed


def _run_briefing(repo: str, gh: str) -> dict:
    script = os.path.join(os.path.dirname(__file__),
                          "..", "..", "..", "core_cli",
                          "briefing_summary.py")
    script = os.path.normpath(script)
    # cwd = live checkout: td discovers project_config.json upward from
    # cwd, and the image workdir (/app) has none. Script stays baked.
    proc = subprocess.run(
        ["python3", script, "--briefing-json",
         "--repo", repo, "--gh", gh],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=180, text=True, cwd=stack_dir())
    # The script exits 1 when no main source worked (no PRs + no
    # board), but its stdout JSON is still complete and carries the
    # structured errs/empties the app renders. Only fail when stdout
    # is unusable.
    try:
        return json.loads(proc.stdout)
    except ValueError:
        pass
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or "").strip().splitlines()[-1]
            if proc.stderr.strip() else "briefing failed")
    raise RuntimeError("briefing returned invalid JSON")


@router.get("/briefing", response_model=Briefing)
def briefing(repo: str = "remote-server") -> dict:
    try:
        name = validate_repo_name(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Fixture override (#453): the explicit env var always wins; a
    # fixture-briefing.json next to the checkouts only applies with
    # the BUTTONS_FIXTURE_AUTO_DISCOVER opt-in. Fixture serves are
    # marked and logged — never silent.
    fixture_path = resolve_briefing_fixture_path()
    if fixture_path and not os.path.exists(fixture_path):
        logger.warning(
            "briefing: fixture %s for repo %s missing; serving live",
            fixture_path, name)
        fixture_path = ""
    if not fixture_path:
        stray = briefing_fixture_candidate()
        if os.path.exists(stray):
            logger.warning(
                "briefing: ignoring stray fixture %s for repo %s "
                "(set BUTTONS_FIXTURE_BRIEFING or "
                "BUTTONS_FIXTURE_AUTO_DISCOVER=1 to serve fixtures)",
                stray, name)
    if fixture_path:
        try:
            with open(fixture_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data["repo"] = name
                data["fixture"] = True
                data["fixture_source"] = fixture_path
                # Fixtures bypass the cache (fast already) but carry
                # the same freshness markers for a uniform contract.
                data["cached"] = False
                data["stale"] = False
                data["generated_at"] = time.time()
            logger.warning(
                "briefing: serving fixture %s for repo %s",
                fixture_path, name)
            return data
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"fixture briefing failed: {exc}")

    try:
        return get_briefing(name, f"arii/{name}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])


@router.post("/briefing/refresh", response_model=BriefingRefreshResult)
def briefing_refresh(body: BriefingRefreshRequest) -> dict:
    """Trigger background regeneration for one repo (#929).

    Deduped via _BRIEFING_REFRESHING: started is False when a
    refresh was already in flight. Non-destructive, so no
    confirm gate (reads stay reads; this just warms them).
    """
    try:
        name = validate_repo_name(body.repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    with _BRIEFING_LOCK:
        already = name in _BRIEFING_REFRESHING
    _revalidate_briefing(name, f"arii/{name}")
    return {"ok": True, "repo": name, "started": not already}


@router.get("/status", response_model=BackendStatus)
def status() -> dict:
    return {"ok": True, "service": "backend", "runs": [],
            "webhooks": {}, "health": get_system_health()}


@router.get("/ledger", response_model=LedgerList)
def ledger(kind: str = "all", limit: int = 50,
           since: float = 0, source: str = "all") -> dict:
    if kind != "all" and kind not in LEDGER_KINDS:
        raise HTTPException(status_code=400, detail="unknown kind")
    if source not in ("all", "console", "cli"):
        raise HTTPException(status_code=400, detail="unknown source")
    limit = max(1, min(200, limit))
    since = max(0.0, since)
    return {"entries": ledger_read(kind, limit, since, source)}


@router.post("/ledger/clear", response_model=LedgerClearResult)
def ledger_clear_route(body: LedgerClearRequest | None = None) -> dict:
    from backend.core import require_confirm

    # Same deliberate-confirm gate as jobs/clear (#454): the body
    # defaults to None (not a required model) so a bare POST with no
    # body also lands on the 400 gate instead of a 422 shape error.
    require_confirm(body, "ledger-clear")
    if ledger_clear():
        return {"ok": True}
    raise HTTPException(status_code=500, detail="ledger clear failed")
