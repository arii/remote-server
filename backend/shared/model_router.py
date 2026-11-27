"""Deterministic-first model routing for S5 agent slices.

Greenfield rule: try deterministic commands first (td/gh/subprocess JSON)
and reach for an LLM only for judgment/explanation/summarization. When an
LLM is needed, this module picks the cheapest feasible one:

  1. S5_LOCAL_ONLY=1 forces local Ollama, otherwise explicit prefer wins,
  2. otherwise local Ollama via TwT (``ASK_MODEL``, default qwen3:8b) when reachable,
  3. otherwise Tokens with Taste's free-first, Nebius-last waterfall.

Explicit OpenRouter/Nebius preferences remain available for diagnostics;
the automatic route never connects directly to either provider.

Stdlib only on purpose: system-python tests import this without the
deep-agent venv. Never touches secrets — no API keys in specs, kwargs, or
logs; the caller passes its own key to ChatOpenAI from the environment.

Env contract:
  ASK_MODEL (default qwen3:8b), OLLAMA_URL (default http://ollama:11434),
  S5_LOCAL_ONLY=1 (Ollama only; reject cloud models and remote overrides),
  OLLAMA_NUM_CTX (optional Ollama context window; server default when unset),
  TWT_BASE_URL (default http://127.0.0.1:8000/v1); local calls use
  the sibling /local native-Ollama endpoint of twt_service.py,
  NEBIUS_MODEL (default zai-org/GLM-5.3-Flash),
  NEBIUS_BASE_URL (default https://api.tokenfactory.nebius.com/v1),
  NEBIUS_API_KEY (required for the nebius path),
  OPENROUTER_API_KEY (required for OpenRouter), OPENROUTER_MODEL
  (default openrouter/free), OPENROUTER_ALLOW_PAID=1 (allow paid models),
  NEBIUS_ONLY=1 (legacy force-remote flag; now selects TwT),
  OLLAMA_STRICT=1 (fail instead of falling back).
  NEBIUS_SPEND_CAP_USD (lifetime metered cap, default $5.00
  when unset; 0 blocks every metered call).

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

import json
import logging
import os
import re
import sys

from backend.shared import twt_transport
import time
import urllib.request

from backend.shared import ledger
from backend.shared import pricing
from backend.shared import runs
from backend.shared.pricing import PROVIDERS

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://ollama:11434"
DEFAULT_OPENROUTER_MODEL = "openrouter/free"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_NEBIUS_MODEL = "zai-org/GLM-5.3-Flash"
DEFAULT_NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
DEFAULT_TWT_BASE_URL = "http://127.0.0.1:8000/v1"
# The TwT gateway accepts provider_models only on the default route.
TWT_DEFAULT_MODEL = "default"
TWT_DEFAULT_ROUTES = frozenset({TWT_DEFAULT_MODEL, "route:default"})
TWT_STRONG_ROUTE = "route:strong"
PIPELINE_MAX_TOKENS = 8192
PIPELINE_HISTORY_CHARS = 64000


def bounded_history(messages):
    """Bound model input without mutating checkpoint history or splitting tools."""

    def size(items):
        return sum(len(json.dumps(m.model_dump(), default=str)) for m in items)

    result = list(messages)
    if size(result) <= PIPELINE_HISTORY_CHARS:
        return result
    for index, message in enumerate(result):
        if message.type in ("tool", "ai") and len(str(message.content)) > 4096:
            text = str(message.content)
            result[index] = message.model_copy(
                update={"content": text[:2048] + "\n[history trimmed]\n" + text[-2048:]}
            )
    groups = []
    for message in result:
        if message.type == "tool" and groups:
            groups[-1].append(message)
        else:
            groups.append([message])
    while size([m for group in groups for m in group]) > PIPELINE_HISTORY_CHARS:
        removable = next(
            (i for i, group in enumerate(groups[:-1]) if group[0].type == "ai"),
            None,
        )
        if removable is None:
            break
        groups.pop(removable)
    # The live tail group cannot be dropped — its tool results answer
    # tool_calls the model must still see resolved — but it can bust the
    # budget alone when one turn fans out dozens of reads (#1571). Shrink
    # those contents to a stub before failing; instruction messages
    # (system/human) are never touched, so an oversized prompt still raises.
    flat = [message for group in groups for message in group]
    cap = 2048
    while size(flat) > PIPELINE_HISTORY_CHARS and cap >= 256:
        for index, message in enumerate(flat):
            if message.type in ("tool", "ai") and len(str(message.content)) > cap:
                text = str(message.content)
                flat[index] = message.model_copy(
                    update={
                        "content": text[: cap // 2]
                        + "\n[history trimmed]\n"
                        + text[-cap // 2 :]
                    }
                )
        cap //= 2
    if size(flat) > PIPELINE_HISTORY_CHARS:
        for index, message in enumerate(flat):
            if message.type in ("tool", "ai"):
                flat[index] = message.model_copy(
                    update={"content": "[history trimmed: oversized result]"}
                )
    if size(flat) > PIPELINE_HISTORY_CHARS:
        raise ValueError("Pipeline history exceeds the instruction/context budget")
    return flat


class _BoundedHistory:
    def _bounded_kwargs(self, kwargs):
        kwargs = dict(kwargs)
        if hasattr(self, "num_predict") and "options" in kwargs:
            options = dict(kwargs["options"] or {})
            value = options.get("num_predict", PIPELINE_MAX_TOKENS)
            if type(value) is not int or value <= 0:
                raise ValueError("Pipeline output budget must be positive")
            options["num_predict"] = min(value, PIPELINE_MAX_TOKENS)
            kwargs["options"] = options
        if hasattr(self, "max_tokens"):
            for key in ("max_tokens", "max_completion_tokens"):
                if key in kwargs:
                    value = kwargs[key]
                    if type(value) is not int or value <= 0:
                        raise ValueError("Pipeline output budget must be positive")
                    kwargs[key] = min(value, PIPELINE_MAX_TOKENS)
        return kwargs

    def _generate(self, messages, *args, **kwargs):
        from backend.shared import twt_receipts

        token = twt_receipts.latest.set(None)
        dispatched = False
        try:
            messages = bounded_history(messages)
            kwargs = self._bounded_kwargs(kwargs)
            dispatched = True
            result = super()._generate(messages, *args, **kwargs)
            return twt_receipts.tag(result)
        except BaseException as error:
            error.twt_trace_id = twt_receipts.latest.get()
            error.llm_not_dispatched = not dispatched
            raise
        finally:
            twt_receipts.latest.reset(token)

    async def _agenerate(self, messages, *args, **kwargs):
        from backend.shared import twt_receipts

        token = twt_receipts.latest.set(None)
        dispatched = False
        try:
            messages = bounded_history(messages)
            kwargs = self._bounded_kwargs(kwargs)
            dispatched = True
            result = await super()._agenerate(messages, *args, **kwargs)
            return twt_receipts.tag(result)
        except BaseException as error:
            error.twt_trace_id = twt_receipts.latest.get()
            error.llm_not_dispatched = not dispatched
            raise
        finally:
            twt_receipts.latest.reset(token)


def _default_ollama_model():
    """The one default model name (D2): defined in
    backend/exec/turn_engine.py; this module only surfaces it
    through ASK_MODEL. Import is lazy so the module stays
    importable where the repo root is not on sys.path."""
    from backend.exec.turn_engine import DEFAULT_MODEL

    return DEFAULT_MODEL


def _env(name, default=""):
    val = os.environ.get(name, default)
    if not isinstance(val, str) or not val.strip():
        return default
    return val


def _ollama_base(base_url=None):
    """Bare Ollama host: OLLAMA_URL ships with the /api/generate
    path in compose, so strip it (same convention as
    backend/repair_worker.py and the briefing router)."""
    base = (base_url or _env("OLLAMA_URL", DEFAULT_OLLAMA_URL)).strip().rstrip("/")
    if base.lower().endswith("/api/generate"):
        base = base[: -len("/api/generate")]
    return base


def ollama_reachable(base_url=None, timeout=5):
    """True when the Ollama API answers, False on any failure."""
    base = _ollama_base(base_url)
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=timeout) as res:
            return res.status == 200
    except Exception:
        return False


def _ollama_spec():
    return {
        "provider": "ollama",
        "model": _env("ASK_MODEL", _default_ollama_model()),
        "base_url": _env("TWT_BASE_URL", DEFAULT_TWT_BASE_URL)
        .rstrip("/")
        .removesuffix("/v1")
        + "/local",
    }


def local_only():
    return _env("S5_LOCAL_ONLY") == "1"


def validate_local_spec(spec):
    if local_only():
        if spec.get("provider") != "ollama":
            raise RuntimeError("S5_LOCAL_ONLY=1 requires Ollama")
        if "cloud" in spec.get("model", "").lower():
            raise RuntimeError("S5_LOCAL_ONLY=1 rejects Ollama cloud models")
    return spec


def api_key(spec):
    """Credential for the resolved remote backend. Keep out of specs and logs."""
    provider = spec["provider"]
    if provider == "twt":
        return "twt-loopback"
    if provider not in ("nebius", "openrouter"):
        raise ValueError(f"bad remote provider: {provider!r}")
    variable = f"{provider.upper()}_API_KEY"
    key = _env(variable).strip()
    if not key:
        raise RuntimeError(
            f"{provider.title()} fallback needs {variable} in the environment"
        )
    return key


def _remote_spec(provider, model, base_url):
    spec = {"provider": provider, "model": model, "base_url": base_url}
    api_key(spec)
    cap = pricing.spend_cap()
    # Effective cap, logged each time the metered path initializes
    # (#885). Info, not warning: the outage signal below stays the
    # one warning per failover.
    logger.info(
        "%s effective spend cap: $%s",
        provider,
        "unlimited" if cap is None else f"{cap:.2f}",
    )
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
                f"{provider.title()} spend cap unverifiable: {total['unknown']} "
                "metered run(s) have no rate; add a rate card entry "
                "or set METERED_RATE_IN_PER_1M/METERED_RATE_OUT_PER_1M"
            )
        spent = total["spent"]
        if spent >= cap:
            raise RuntimeError(
                f"{provider.title()} spend cap hit: ${spent:.4f} of ${cap:.4f} "
                "used; raise NEBIUS_SPEND_CAP_USD to continue"
            )
    return spec


def _nebius_spec():
    return _remote_spec(
        "nebius",
        _env("NEBIUS_MODEL", DEFAULT_NEBIUS_MODEL),
        _env("NEBIUS_BASE_URL", DEFAULT_NEBIUS_BASE_URL),
    )


def _twt_spec():
    return _remote_spec(
        "twt", TWT_DEFAULT_MODEL, _env("TWT_BASE_URL", DEFAULT_TWT_BASE_URL)
    )


def _validate_openrouter_model(model):
    if (
        not pricing.is_free_openrouter(f"openrouter:{model}")
        and _env("OPENROUTER_ALLOW_PAID") != "1"
    ):
        raise RuntimeError(
            "OpenRouter requires a free model unless OPENROUTER_ALLOW_PAID=1"
        )


def _openrouter_spec():
    model = _env("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL).strip()
    _validate_openrouter_model(model)
    return _remote_spec("openrouter", model, DEFAULT_OPENROUTER_BASE_URL)


def _note_substitution(spec, requested):
    """Mark and warn when the served provider differs from the requested one.

    Which model wins is unchanged; the spec gains requested/served_by/
    substituted so callers and the ledger can audit the swap.
    """
    if not requested or spec["provider"] == requested:
        return spec
    logger.warning(
        "model_router: requested provider %s but serving %s (%s)",
        requested,
        spec["provider"],
        spec.get("model", "?"),
    )
    return dict(spec, requested=requested, served_by=spec["provider"], substituted=True)


def resolve_llm(prefer=None):
    """Pick the cheapest feasible LLM. Returns {"provider","model","base_url"}.

    When the served provider differs from the requested one (``prefer``,
    NEBIUS_ONLY, or S5_ISSUE_PROVIDER), the spec also carries
    ``requested``, ``served_by`` and ``substituted`` and a warning is logged.

    ``prefer`` selects a provider strictly. Default order is local Ollama,
    then TwT. TwT owns the free-tier/paid waterfall.
    OLLAMA_STRICT=1 disables the fallback on the default path too.
    """
    requested = (
        (
            prefer
            or ("twt" if _env("NEBIUS_ONLY") == "1" else "")
            or _env("S5_ISSUE_PROVIDER")
        )
        .strip()
        .lower()
    )
    if local_only():
        spec = validate_local_spec(_ollama_spec())
        if not ollama_reachable():
            raise RuntimeError(
                "S5_LOCAL_ONLY=1: Ollama unreachable at " + _ollama_base()
            )
        return _note_substitution(spec, requested)
    only_nebius = _env("NEBIUS_ONLY") == "1"
    strict_ollama = _env("OLLAMA_STRICT") == "1"
    want = (prefer or ("twt" if only_nebius else "")).strip().lower()
    if want and want not in PROVIDERS:
        raise ValueError(f"bad provider {prefer!r}: want {', '.join(PROVIDERS)}")
    if want == "nebius":
        return _nebius_spec()
    if want == "openrouter":
        return _openrouter_spec()
    if want == "twt":
        return _twt_spec()
    if want == "ollama":
        if ollama_reachable():
            return _ollama_spec()
        raise RuntimeError(
            "Ollama unreachable at " + _env("OLLAMA_URL", DEFAULT_OLLAMA_URL)
        )
    if ollama_reachable():
        return _note_substitution(_ollama_spec(), requested)
    if strict_ollama:
        raise RuntimeError(
            "Ollama unreachable and OLLAMA_STRICT=1 refuses remote fallback"
        )
    spec = _twt_spec()
    _announce_fallback(spec)
    return spec


def _announce_fallback(spec):
    """Announce default remote selection and record a zero-usage audit marker."""
    url = _env("OLLAMA_URL", DEFAULT_OLLAMA_URL)
    model = spec.get("model", "?")
    provider = spec["provider"]
    label = provider
    logger.warning("ollama unreachable at %s, using %s (%s)", url, label, model)
    print(
        f"model_router: ollama unreachable at {url}, using {label} ({model})",
        file=sys.stderr,
    )
    # Unknown paid model rates still fail closed after the marker.
    ledger.record(
        script="model_router",
        model=provider + ":" + str(model),
        thread_id="",
        messages=[],
        started=time.time(),
        extra={
            "event": "metered-failover",
            "reason": "ollama unreachable",
            "llm_call": False,
        },
        usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        calls=[],
    )


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


def with_local_fallback(primary_fn, make_local_fn, note_fn, *, primary_is_local=False):
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
    validate_local_spec(spec)
    if spec.get("provider") == "ollama":
        return {
            "model": spec["model"],
            "base_url": spec["base_url"],
            "temperature": 0,
            "num_predict": PIPELINE_MAX_TOKENS,
            "disable_streaming": True,
        }
    if spec.get("provider") == "twt":
        return {
            "model": spec["model"],
            "base_url": spec["base_url"],
            "temperature": 0,
            "include_response_headers": True,
            "disable_streaming": True,
            "max_retries": 0,
            "max_tokens": PIPELINE_MAX_TOKENS,
            **(twt_transport.openai_clients() if twt_transport.uds_path() else {}),
        }
    if spec.get("provider") in ("nebius", "openrouter"):
        if spec["provider"] == "openrouter":
            _validate_openrouter_model(spec["model"])
        kwargs = {
            "model": spec["model"],
            "base_url": spec["base_url"],
            "temperature": 0,
            # The gateway omits usage from stream chunks unless asked;
            # without this streamed runs lose token usage.
            "stream_usage": True,
        }
        if spec["provider"] == "nebius":
            # #1040: leave output budget for visible answers.
            kwargs.update({"reasoning_effort": "low"})
        return kwargs
    raise ValueError(f"bad spec provider: {spec.get('provider')!r}")


# deepagents resolves harness profiles by the BUILT instance's
# (provider, identifier): ChatOllama -> ollama:<model>, ChatOpenAI
# (our Nebius path) -> openai:<model>. Translate our spec provider
# names; anything else passes through for custom model classes.
# (Mirrors deep_chat.PROFILE_PROVIDERS for the chat gate.)
PROFILE_PROVIDERS = {
    "ollama": "ollama",
    "nebius": "openai",
    "openrouter": "openai",
    "twt": "openai",
}


PIPELINE_STAGES = frozenset(("coder", "spec", "audit"))


def _gateway_strong_models():
    """Parse TWT_STRONG_MODELS (the gateway's provider->model JSON), or None if unset."""
    from backend.shared import twt_provenance

    raw = _env("TWT_STRONG_MODELS")
    if not raw:
        return None
    guidance = (
        "TWT_STRONG_MODELS must be a non-empty JSON object of provider -> model id "
        "(same value the gateway uses for route:strong)"
    )
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise twt_provenance.TwTPreflightError(
            f"{guidance}: invalid JSON ({exc})"
        ) from exc
    if (
        not isinstance(parsed, dict)
        or not parsed
        or not all(
            isinstance(k, str) and k and isinstance(v, str) and v.strip()
            for k, v in parsed.items()
        )
    ):
        raise twt_provenance.TwTPreflightError(f"{guidance}; got {raw[:200]!r}")
    return parsed


def preflight_gateway(spec=None, expected_sha=None, stage=None):
    """Preflight check for TwT gateway capabilities and provenance before paid inference."""
    spec = dict(spec) if spec else resolve_llm()
    if stage is not None and stage not in PIPELINE_STAGES:
        raise ValueError(f"unknown pipeline model stage: {stage}")
    if spec.get("provider") != "twt":
        return
    from backend.shared import twt_provenance

    base_url = spec.get("base_url") or _env("TWT_BASE_URL", DEFAULT_TWT_BASE_URL)
    uds = spec.get("uds") or (_env("TWT_UDS") or None)
    model = spec.get("model")
    is_default_route = model in TWT_DEFAULT_ROUTES
    provider_models = (
        dict(spec.get("provider_models") or {}) if is_default_route else {}
    )
    if stage is not None and not provider_models and is_default_route:
        for provider_name in (
            "nvidia",
            "nebius",
            "google",
            "groq",
            "openrouter",
            "sambanova",
            "cloudflare",
        ):
            override = _env(f"TWT_{stage.upper()}_{provider_name.upper()}_MODEL")
            if override:
                provider_models[provider_name] = override
    extra = {}
    if model == TWT_STRONG_ROUTE:
        # route:strong is resolved gateway-side and is absent from /v1/models;
        # validate the gateway's own strong map when we can see it.
        provider_models = _gateway_strong_models()
    elif model and not is_default_route:
        extra["model_ids"] = [model]
    sha = expected_sha or _env("TWT_EXPECTED_SHA")
    twt_provenance.verify_gateway_capabilities(
        base_url=base_url,
        uds=uds,
        provider_models=provider_models,
        expected_sha=sha,
        **extra,
    )


def build_chat_model(
    spec=None, think=False, num_ctx=None, stage=None, preflight=False, max_tokens=None
):
    """Container-correct chat model (Ollama no-think default). Venv-only.

    resolve_llm() picks local or keyed remote through the spend choke;
    chat_kwargs() supplies the
    provider-correct constructor args including the in-container
    Ollama base_url. Returns (model, spec). max_tokens is an optional
    per-caller output budget, clamped to PIPELINE_MAX_TOKENS.
    """
    spec = dict(spec) if spec else resolve_llm()
    if stage is not None and stage not in PIPELINE_STAGES:
        raise ValueError(f"unknown pipeline model stage: {stage}")
    is_twt_default = (
        spec.get("provider") == "twt" and spec.get("model") in TWT_DEFAULT_ROUTES
    )
    if is_twt_default and stage is not None:
        provider_models = {}
        for provider_name in (
            "nvidia",
            "nebius",
            "google",
            "groq",
            "openrouter",
            "sambanova",
            "cloudflare",
        ):
            override = _env(f"TWT_{stage.upper()}_{provider_name.upper()}_MODEL")
            if override:
                provider_models[provider_name] = override
        if provider_models:
            spec["provider_models"] = provider_models
        else:
            spec.pop("provider_models", None)
        spec.pop("provider_reasoning_effort", None)
    elif spec.get("provider") == "twt":
        spec.pop("provider_models", None)
        spec.pop("provider_reasoning_effort", None)
    kwargs = chat_kwargs(spec)
    if max_tokens is not None:
        budget_key = "num_predict" if spec.get("provider") == "ollama" else "max_tokens"
        kwargs[budget_key] = min(max_tokens, PIPELINE_MAX_TOKENS)
    if is_twt_default and spec.get("provider_models"):
        kwargs["extra_body"] = {"provider_models": spec["provider_models"]}
        if spec.get("provider_reasoning_effort"):
            kwargs["extra_body"]["provider_reasoning_effort"] = spec[
                "provider_reasoning_effort"
            ]
    if preflight or _env("TWT_PREFLIGHT_REQUIRED") == "1":
        preflight_gateway(spec=spec, stage=stage)
    from backend.shared import twt_receipts

    if spec.get("provider") == "ollama":
        from langchain_ollama import ChatOllama  # noqa: E402

        kwargs["reasoning"] = think
        kwargs["sync_client_kwargs"] = twt_transport.client_kwargs(
            hook=twt_receipts.capture
        )
        kwargs["async_client_kwargs"] = twt_transport.client_kwargs(
            asynchronous=True, hook=twt_receipts.acapture
        )
        if num_ctx is None and _env("OLLAMA_NUM_CTX"):
            try:
                num_ctx = int(_env("OLLAMA_NUM_CTX"))
            except ValueError as exc:
                raise ValueError("OLLAMA_NUM_CTX must be a positive integer") from exc
            if num_ctx <= 0:
                raise ValueError("OLLAMA_NUM_CTX must be a positive integer")
        if num_ctx is not None:
            kwargs["num_ctx"] = num_ctx

        class PipelineOllama(_BoundedHistory, ChatOllama):
            pass

        return PipelineOllama(**kwargs), spec
    from langchain_openai import ChatOpenAI  # noqa: E402

    if spec.get("provider") == "twt" and "http_client" not in kwargs:
        kwargs.update(twt_transport.openai_clients())

    class PipelineOpenAI(_BoundedHistory, ChatOpenAI):
        def _get_request_payload(self, *args, **kwargs):
            payload = super()._get_request_payload(*args, **kwargs)
            limit = payload.pop("max_completion_tokens", PIPELINE_MAX_TOKENS)
            payload["max_tokens"] = min(limit, PIPELINE_MAX_TOKENS)
            return payload

    return PipelineOpenAI(api_key=api_key(spec), **kwargs), spec


def build_structured_chat_model(
    spec=None, schema=None, think=False, include_raw=False, stage=None
):
    """Native structured-output chat model (no prompt-hinting).

    Binds the Pydantic schema at the protocol layer: Ollama gets
    format=<json schema> (server-side constrained decoding), Nebius
    gets response_format json_schema strict=True. Drops ~150-300
    prompt/format-repair tokens per verdict turn (Gemini round
    docs/gemini-round-2026-11-09.md). Returns (model, spec).
    """
    model, resolved = build_chat_model(spec, think=think, stage=stage)
    if schema is None:
        return model, resolved
    kwargs = {"method": "json_schema"}
    if include_raw:
        kwargs["include_raw"] = True
    if resolved.get("provider") in ("nebius", "openrouter", "twt"):
        kwargs["strict"] = True
    return model.with_structured_output(schema, **kwargs), resolved


build_structured_model = build_structured_chat_model


def disable_general_purpose(spec):
    """Disable the auto-added general-purpose subagent for one model.

    Venv-only (imports deepagents). Registers an exact
    provider:model harness profile with the default general-purpose
    subagent disabled, so create_deep_agent skips the auto-add: with
    no other sync subagents the task tool is not exposed at all;
    callers passing subagents= (triage scouts) keep task with only
    their own types. No delegation hop, no second model spend, no
    ungated file tools outside the builder's own contract. (#539)
    """
    from deepagents import (  # noqa: E402
        GeneralPurposeSubagentProfile,
        HarnessProfileConfig,
        register_harness_profile,
    )

    provider = PROFILE_PROVIDERS.get(spec["provider"], spec["provider"])
    register_harness_profile(
        f"{provider}:{spec['model']}",
        HarnessProfileConfig(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)
        ),
    )
