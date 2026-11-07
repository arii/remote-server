#!/usr/bin/env bash
# cutoff-drill.sh — fire-drill for the Muse cutoff (issue #800).
#
# Masks the `muse` binary with a failing tripwire shim, then exercises
# the four console surfaces that must survive cutoff and reports
# per-surface ok/fail/skip:
#   chat-turn-agy     agy argv has no muse token; cutoff flip works
#   ask-pipeline      model routing resolves without muse; Ollama live?
#   briefing-narrator narrator defaults to ollama; prompt builds offline
#   eval-gate         eval gates load their cases with muse masked
#
# Default run is cheap and offline-safe: static checks plus presence
# probes (agy binary in terminal, Ollama /api/tags). No LLM spend.
# --live additionally runs one real agy print-mode turn and one real
# narrator call (metered/slow; needs the stack up with models pulled).
# --proof (issue #889) is the CI keyless proof: --live plus a
# credential scrub (NEBIUS_API_KEY, GEMINI_API_KEY, ...) asserted
# absent, OLLAMA_STRICT=1, and skips treated as failures. The live
# agy turn stays out of proof mode — it needs stack OAuth, which a
# keyless CI job has no business holding; the argv+flip static proof
# stands as the agy surface there. Run --proof locally with Ollama
# up for the same proof CI runs.
#
# A FAIL means a hidden Muse dependency: file a follow-up issue per
# red surface (do not fix it in the drill PR). Any tripwire hit —
# a surface shelling out to `muse` — fails the drill outright.
#
# Usage: ./scripts/cutoff-drill.sh [--live | --proof]
set -euo pipefail

LIVE=0
PROOF=0
if [[ "${1:-}" == "--live" ]]; then
  LIVE=1
elif [[ "${1:-}" == "--proof" ]]; then
  LIVE=1
  PROOF=1
elif [[ -n "${1:-}" ]]; then
  echo "usage: $0 [--live | --proof]" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

FAIL=0
report() { # <status> <surface> <detail...>
  local status="$1" surface="$2"
  shift 2
  printf '%-4s %-16s %s\n' "$status" "$surface" "$*"
}
ok() { report ok "$@"; }
skip() {
  if [[ "$PROOF" == "1" ]]; then
    report FAIL "$@"
    FAIL=1
  else
    report SKIP "$@"
  fi
}
fail() { report FAIL "$@"; FAIL=1; }

# Proof mode runs every surface with cloud credentials stripped.
# Scrub first so the surfaces inherit the keyless env, then assert
# the vars are actually absent — the proof is the assertion, not
# the assumption. OLLAMA_STRICT=1 refuses the Nebius fallback on
# the default path, so an Ollama-only violation fails instead of
# going metered. NEBIUS_ONLY is scrubbed too: it forces cloud.
CUTOFF_SCRUB_VARS=(NEBIUS_API_KEY GEMINI_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY LANGSMITH_API_KEY NEBIUS_ONLY)
if [[ "$PROOF" == "1" ]]; then
  was_set=""
  for v in "${CUTOFF_SCRUB_VARS[@]}"; do
    if printenv "$v" >/dev/null 2>&1; then was_set="$was_set $v"; fi
    unset "$v" 2>/dev/null || true
  done
  export OLLAMA_STRICT=1
  still_set=""
  for v in "${CUTOFF_SCRUB_VARS[@]}"; do
    if printenv "$v" >/dev/null 2>&1; then still_set="$still_set $v"; fi
  done
  if [[ -n "$still_set" ]]; then
    fail proof "credential env still set after scrub:$still_set"
  elif [[ -n "$was_set" ]]; then
    ok proof "scrubbed credential env:$was_set; OLLAMA_STRICT=1"
  else
    ok proof "env already keyless; OLLAMA_STRICT=1"
  fi
fi

# Mask: a `muse` shim first on PATH that fails like a missing binary
# and records any invocation. A surface that shells out to muse trips
# it; the drill fails at the end when the trip file is non-empty.
MASKDIR="$(mktemp -d)"
trap 'rm -rf "$MASKDIR"' EXIT
cat >"$MASKDIR/muse" <<'EOF'
#!/usr/bin/env bash
echo "tripped: muse $*" >>"${CUTOFF_TRIP:?}"
echo "muse: masked by cutoff drill" >&2
exit 127
EOF
chmod +x "$MASKDIR/muse"
export CUTOFF_TRIP="$MASKDIR/tripped"
: >"$CUTOFF_TRIP"
export PATH="$MASKDIR:$PATH"

# --- surface 1: chat turn (agy) ---
AGY_STATIC="$(python3 - <<'EOF'
import os
import sys
sys.path.insert(0, ".")
from backend.exec.agent_turn import ordered_providers, turn_argv
failures = []
for continued in (False, True):
    argv = turn_argv("agy", "/srv/remote-server", "00000000-0000-0000-0000-000000000000",
                    "hi", continued, "cid-1" if continued else None)
    if any("muse" in str(tok).lower() for tok in argv):
        failures.append(f"agy argv carries muse: {argv}")
os.environ["AGENT_DEFAULT_PROVIDER"] = "agy"
if ordered_providers()[0] != "agy":
    failures.append("AGENT_DEFAULT_PROVIDER=agy did not flip first")
os.environ.pop("AGENT_DEFAULT_PROVIDER", None)
print("default-order:" + ",".join(ordered_providers()))
for f in failures:
    print("FAIL:" + f)
EOF
)"
if grep -q "^FAIL:" <<<"$AGY_STATIC"; then
  fail chat-turn-agy "$(grep "^FAIL:" <<<"$AGY_STATIC" | head -n 1)"
else
  ok chat-turn-agy "argv muse-free; flip works ($(grep "^default-order:" <<<"$AGY_STATIC" | cut -d: -f2-) default)"
fi

if [[ "$PROOF" == "1" ]]; then
  ok chat-turn-agy "proof mode: argv+flip static proof (live turn needs stack OAuth; run --live locally)"
elif command -v docker >/dev/null 2>&1 && docker compose exec -T terminal command -v agy >/dev/null 2>&1; then
  ok chat-turn-agy "agy binary present in terminal"
else
  skip chat-turn-agy "terminal/agy not reachable (stack down?)"
fi

if [[ "$LIVE" == "1" && "$PROOF" == "0" ]]; then
  if docker compose exec -T terminal agy --output-format stream-json -p "Reply with exactly: PONG" 2>/dev/null | grep -q "PONG"; then
    ok chat-turn-agy "live print-mode turn returned PONG"
  else
    fail chat-turn-agy "live agy turn failed (see docs/agy-continuity.md)"
  fi
fi

# --- surface 2: ask pipeline ---
ASK_STATIC="$(python3 - <<'EOF'
import sys
sys.path.insert(0, "deep-agent")
import model_router
failures = []
for mod in ("model_router.py", "ask_investigator.py", "deep_chat.py"):
    with open(f"deep-agent/{mod}", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            low = line.lower()
            # .muse/ is a grep exclusion dir, not a dependency.
            if "muse" in low and ".muse" not in low:
                failures.append(f"{mod}:{n}: {line.strip()[:100]}")
for bad in ("muse", "Muse"):
    try:
        model_router.resolve_llm(prefer=bad)
        failures.append(f"resolve_llm(prefer={bad!r}) accepted")
    except ValueError:
        pass
for f in failures:
    print("FAIL:" + f)
print("checked:4")
EOF
)"
if grep -q "^FAIL:" <<<"$ASK_STATIC"; then
  fail ask-pipeline "$(grep "^FAIL:" <<<"$ASK_STATIC" | head -n 1)"
else
  ok ask-pipeline "ollama/nebius only; muse prefer rejected"
fi

ASK_LIVE="$(OLLAMA_STRICT=1 python3 -c "
import sys
sys.path.insert(0, 'deep-agent')
import model_router
try:
    spec = model_router.resolve_llm()
    print('resolved:' + spec['provider'] + ':' + spec['model'])
except RuntimeError as exc:
    print('unreachable:' + str(exc)[:80])
" 2>/dev/null || echo "unreachable:probe error")"
if [[ "$ASK_LIVE" == resolved:* ]]; then
  ok ask-pipeline "llm resolves ${ASK_LIVE#resolved:} with muse masked"
else
  skip ask-pipeline "ollama unreachable (${ASK_LIVE#unreachable:})"
fi

# --- surface 3: briefing narrator ---
NARR_STATIC="$(python3 - <<'EOF'
import os
import sys
sys.path.insert(0, ".")
failures = []
with open("backend/api/routers/briefing.py", encoding="utf-8") as fh:
    for n, line in enumerate(fh, 1):
        if "muse" in line.lower():
            failures.append(f"briefing.py:{n}: {line.strip()[:100]}")
with open("backend/briefing_summary.py", encoding="utf-8") as fh:
    for n, line in enumerate(fh, 1):
        if "muse" in line.lower():
            failures.append(f"briefing_summary.py:{n}: {line.strip()[:100]}")
os.environ.pop("BRIEFING_NARRATOR", None)
from backend.api.routers.briefing import narrator_prompt, narrator_spec
spec = narrator_spec()
if spec is None or spec.get("provider") != "ollama":
    failures.append(f"default narrator spec is not ollama: {spec}")
prompt = narrator_prompt({"repo": "x", "attention": [], "narrative": "old"})
if "old" in prompt or "Briefing JSON" not in prompt:
    failures.append("narrator_prompt shape wrong")
for f in failures:
    print("FAIL:" + f)
print("default:" + str((spec or {}).get("provider")))
EOF
)"
if grep -q "^FAIL:" <<<"$NARR_STATIC"; then
  fail briefing-narrator "$(grep "^FAIL:" <<<"$NARR_STATIC" | head -n 1)"
else
  ok briefing-narrator "muse-free; default $(grep "^default:" <<<"$NARR_STATIC" | cut -d: -f2-); prompt builds offline"
fi

if python3 -c "import sys, urllib.request; sys.path.insert(0,'deep-agent'); import model_router; sys.exit(0 if model_router.ollama_reachable() else 1)" 2>/dev/null; then
  ok briefing-narrator "ollama backend reachable"
else
  skip briefing-narrator "ollama unreachable (narrator backend down)"
fi

if [[ "$LIVE" == "1" ]]; then
  NARR_LIVE="$(python3 - <<'EOF' 2>/dev/null
import sys
sys.path.insert(0, ".")
from backend.api.routers.briefing import narrate_briefing
out = narrate_briefing({"repo": "drill", "attention": ["cutoff drill probe"]}, timeout=300)
print("narrated" if out else "empty")
EOF
)"
  if [[ "$NARR_LIVE" == "narrated" ]]; then
    ok briefing-narrator "live lede generated"
  else
    fail briefing-narrator "live narration empty/failed"
  fi
fi

# --- surface 4: eval gate ---
EVAL_STATIC="$(python3 - <<'EOF'
import sys
sys.path.insert(0, "deep-agent")
failures = []
for mod in ("eval_lib.py", "eval_ask.py", "eval_chat.py"):
    with open(f"deep-agent/{mod}", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if "muse" in line.lower():
                failures.append(f"{mod}:{n}: {line.strip()[:100]}")
import eval_ask
import eval_chat  # noqa: F401  (import proves it loads masked)
import eval_lib
if not eval_ask.PROMPTS:
    failures.append("eval_ask.PROMPTS empty")
exe = eval_lib.resolve_exe("deep-agent")
if not exe:
    failures.append("eval_lib.resolve_exe empty")
for f in failures:
    print("FAIL:" + f)
print(f"cases:{len(eval_ask.PROMPTS)}")
EOF
)"
if grep -q "^FAIL:" <<<"$EVAL_STATIC"; then
  fail eval-gate "$(grep "^FAIL:" <<<"$EVAL_STATIC" | head -n 1)"
else
  ok eval-gate "muse-free; $(grep "^cases:" <<<"$EVAL_STATIC" | cut -d: -f2-) ask cases load masked (live gate is manual, see runbook)"
fi

# --- tripwire verdict ---
if [[ -s "$CUTOFF_TRIP" ]]; then
  fail drill "$(head -n 1 "$CUTOFF_TRIP")"
  FAIL=1
else
  ok drill "muse binary never invoked"
fi

if [[ "$FAIL" == "0" ]]; then
  if [[ "$PROOF" == "1" ]]; then
    echo "cutoff drill: GREEN (keyless proof: muse masked, credentials absent, all surfaces ok)"
  else
    echo "cutoff drill: GREEN (muse masked, all surfaces ok/skipped)"
  fi
else
  echo "cutoff drill: RED — file a follow-up per FAIL line above"
fi
exit "$FAIL"
