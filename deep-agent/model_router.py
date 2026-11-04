"""Deterministic-first model routing for S5 agent slices.

Greenfield rule: try deterministic commands first (td/gh/subprocess JSON)
and reach for an LLM only for judgment/explanation/summarization. When an
LLM is needed, this module picks the cheapest feasible one:

  1. explicit ``prefer`` (or NEBIUS_ONLY=1) wins,
  2. otherwise local Ollama (``ASK_MODEL``, default qwen3:8b) when reachable,
  3. otherwise Nebius (``NEBIUS_MODEL``, default zai-org/GLM-5.3-Flash —
     proven in harness bring-up, Flash tier = cheap) when a key exists,
  4. otherwise raise with an actionable message.

Stdlib only on purpose: system-python tests import this without the
deep-agent venv. Never touches secrets — no API keys in specs, kwargs, or
logs; the caller passes its own key to ChatOpenAI from the environment.

Env contract:
  ASK_MODEL (default qwen3:8b), OLLAMA_URL (default http://ollama:11434),
  NEBIUS_MODEL (default zai-org/GLM-5.3-Flash),
  NEBIUS_BASE_URL (default https://api.tokenfactory.nebius.com/v1),
  NEBIUS_API_KEY (required for the nebius path),
  NEBIUS_ONLY=1 (force cloud), OLLAMA_STRICT=1 (fail instead of falling back).
  NEBIUS_SPEND_CAP_USD (optional lifetime metered cap; 0 blocks
  every metered call, unset means uncapped).

Routing ladder (policy, 2026-11-02):
  deterministic command -> Ollama qwen3:8b -> Ollama qwen3:14b ->
  API-token LLM. The 14b tier is selected today by setting
  ASK_MODEL=qwen3:14b (no separate code path); it becomes the
  default significant-local tier once tok/s is measured on the
  5850U after the terminal rebuild (#246).
  Route to the API-token model only when the task needs
  write-mode multi-step execution, context the 8b window cannot
  hold, or judged output the 8b eval gate does not cover.
  OLLAMA_STRICT=1 keeps the box usable offline (fail, never cloud).
"""

import logging
import os
import re
import sys
import urllib.request

import pricing
import runs

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_MODEL = "qwen3:8b"
DEFAULT_OLLAMA_URL = "http://ollama:11434"
DEFAULT_NEBIUS_MODEL = "zai-org/GLM-5.3-Flash"
DEFAULT_NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"


def _env(name, default=""):
    val = os.environ.get(name, default)
    if not isinstance(val, str) or not val.strip():
        return default
    return val


def ollama_reachable(base_url=None, timeout=5):
    """True when the Ollama API answers, False on any failure."""
    base = (base_url or _env("OLLAMA_URL", DEFAULT_OLLAMA_URL)).rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=timeout) as res:
            return res.status == 200
    except Exception:
        return False


def _ollama_spec():
    return {
        "provider": "ollama",
        "model": _env("ASK_MODEL", DEFAULT_OLLAMA_MODEL),
        "base_url": _env("OLLAMA_URL", DEFAULT_OLLAMA_URL),
    }


def _nebius_spec():
    if not _env("NEBIUS_API_KEY"):
        raise RuntimeError(
            "Nebius fallback needs NEBIUS_API_KEY in the environment"
        )
    cap = pricing.spend_cap()
    if cap is not None:
        # Single choke point: every metered path flows through here
        # (guarded by tests/test_nebius_choke.py — no direct Nebius
        # ChatOpenAI construction outside router-backed builders).
        # Spend is recorded ledger cost (in-flight turns land at
        # turn end, so one turn may cross the line; the next blocks).
        total = runs.total_metered_spend()
        if total["unknown"] > 0:
            # Fail closed: unrated spend is unbounded spend. Rate
            # the models (card or METERED_RATE_* override) to lift.
            raise RuntimeError(
                f"Nebius spend cap unverifiable: {total['unknown']} "
                "metered run(s) have no rate — add a rate card entry "
                "or set METERED_RATE_IN_PER_1M/METERED_RATE_OUT_PER_1M")
        spent = total["spent"]
        if spent >= cap:
            raise RuntimeError(
                f"Nebius spend cap hit: ${spent:.4f} of ${cap:.4f} "
                "used — raise NEBIUS_SPEND_CAP_USD to continue")
    return {
        "provider": "nebius",
        "model": _env("NEBIUS_MODEL", DEFAULT_NEBIUS_MODEL),
        "base_url": _env("NEBIUS_BASE_URL", DEFAULT_NEBIUS_BASE_URL),
    }


def resolve_llm(prefer=None):
    """Pick the cheapest feasible LLM. Returns {"provider","model","base_url"}.

    ``prefer`` is "ollama" or "nebius" (strict: no fallback). With no
    prefer: Ollama when reachable, else Nebius when keyed, else raise.
    OLLAMA_STRICT=1 disables the fallback on the default path too.
    """
    only_nebius = _env("NEBIUS_ONLY") == "1"
    strict_ollama = _env("OLLAMA_STRICT") == "1"
    want = (prefer or ("nebius" if only_nebius else "")).strip().lower()
    if want and want not in ("ollama", "nebius"):
        raise ValueError(f"bad provider {prefer!r}: want 'ollama' or 'nebius'")
    if want == "nebius":
        return _nebius_spec()
    if want == "ollama":
        if ollama_reachable():
            return _ollama_spec()
        raise RuntimeError("Ollama unreachable at " + _env("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    if ollama_reachable():
        return _ollama_spec()
    if strict_ollama:
        raise RuntimeError(
            "Ollama unreachable and OLLAMA_STRICT=1 refuses the Nebius fallback"
        )
    spec = _nebius_spec()
    _announce_fallback(spec)
    return spec


def _announce_fallback(spec):
    """One-line outage signal when the default path goes metered.

    Fires exactly when an Ollama outage reroutes a resolve to paid
    Nebius (never on the explicit prefer="nebius" path — that is a
    choice, not a downgrade). Journals get it via logging, run
    output via stderr. Never logs the spec itself (no-secrets rule:
    model names and URLs only).
    """
    url = _env("OLLAMA_URL", DEFAULT_OLLAMA_URL)
    model = spec.get("model", "?")
    logger.warning("ollama unreachable at %s, using metered nebius (%s)",
                   url, model)
    print(f"model_router: ollama unreachable at {url}, "
          f"using metered nebius ({model})", file=sys.stderr)


# Mid-turn failover (#649): explicit non-5xx codes worth one local
# retry. 409 is deliberately absent — a conflict is a request/state
# problem, not spare capacity, so failing over would burn local
# inference on a doomed request.
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Message shapes for errors that carry no status code (connection and
# timeout errors rarely do). Positive signals only: auth, validation,
# not-found, and content-policy errors match nothing here and never
# fail over.
_RETRYABLE_MSG = re.compile(
    r"rate[\s_-]*limit|too many requests|overloaded|over capacity"
    r"|timed?\s?out|timeout|temporar\w+ unavailable|service unavailable"
    r"|server error|internal error|bad gateway|gateway timeout"
    r"|connection (reset|refused|aborted|error|failed|timed?\s?out)"
    r"|connectionerror|connecterror|try again",
    re.IGNORECASE,
)

# OpenAI-compat / httpx / stdlib exception names that mean transient.
# Searched over the whole MRO so subclasses with unrelated names
# still match (e.g. a custom error extending ConnectionError).
_RETRYABLE_TYPE = re.compile(
    r"RateLimit|TooManyRequests|Timeout|TimedOut|Connect|Connection"
    r"|Overloaded|Unavailable|ServiceUnavailable|InternalServer"
    r"|ServerError|BadGateway|GatewayTimeout",
)


def _as_code(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _status_code(exc):
    """Best-effort HTTP status from OpenAI-compat error shapes.

    Checks status_code/status on the exception itself, then on a
    nested .response (openai.APIStatusError, httpx.HTTPStatusError).
    Returns None when no code is present (connection/timeout errors).
    """
    seen = (exc, getattr(exc, "response", None))
    for obj in seen:
        if obj is None:
            continue
        for attr in ("status_code", "status"):
            code = _as_code(getattr(obj, attr, None))
            if code is not None:
                return code
    return None


def is_retryable_error(exc):
    """True when a failed cloud call is worth one local retry.

    An explicit status code decides: retryable codes (429/408/425
    plus any 5xx) return True, everything else (400/401/403/404/422,
    content-policy rejections) returns False. Without a code, match
    transient type names and message shapes (overloaded, rate limit,
    timeout, connection). Pure, stdlib-only, heavily unit-tested.
    """
    code = _status_code(exc)
    if code is not None:
        return code in RETRYABLE_STATUS_CODES or 500 <= code <= 599
    names = " ".join(cls.__name__ for cls in type(exc).__mro__)
    if _RETRYABLE_TYPE.search(names):
        return True
    return bool(_RETRYABLE_MSG.search(str(exc)))


def with_local_fallback(primary_fn, make_local_fn, note_fn,
                        *, primary_is_local=False):
    """Run primary_fn(); on a retryable cloud error run one local leg.

    Framework-free: primary_fn is a zero-arg thunk for the cloud
    call, make_local_fn builds the local leg and returns a zero-arg
    thunk that runs it, note_fn(exc) announces the failover. Single
    hop only — local-primary calls and the local leg itself never
    re-enter (no ping-pong).

    Order on a retryable error: build local, emit the note, run the
    fallback. Anything else — non-retryable error, local-primary
    call, local build failure, local leg failure — raises the
    ORIGINAL error; a failed local leg chains as __cause__ so the
    primary traceback stays primary and the local error is context.
    """
    try:
        return primary_fn()
    except Exception as exc:
        if primary_is_local or not is_retryable_error(exc):
            raise
        try:
            fallback_fn = make_local_fn()
        except Exception as build_error:
            raise exc from build_error
        note_fn(exc)
        try:
            return fallback_fn()
        except Exception as fallback_error:
            raise exc from fallback_error


def chat_kwargs(spec):
    """Constructor kwargs for ChatOllama / ChatOpenAI. No secrets inside —
    the caller supplies api_key itself, and specs must never be logged."""
    if spec.get("provider") == "ollama":
        return {
            "model": spec["model"],
            "base_url": spec["base_url"],
            "temperature": 0,
        }
    if spec.get("provider") == "nebius":
        return {
            "model": spec["model"],
            "base_url": spec["base_url"],
            "temperature": 0,
            # The gateway omits usage from stream chunks unless asked;
            # without this every Nebius run ledgers zero tokens.
            "stream_usage": True,
        }
    raise ValueError(f"bad spec provider: {spec.get('provider')!r}")
