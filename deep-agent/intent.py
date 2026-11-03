"""Deterministic intent routing for deep_chat turns (roadmap item 3).

classify() maps a prompt to implement/fix/review/explain with
keyword rules only — no LLM call. hint() returns a one-line system
addition nudging the matching tool path, or None for the default
explain intent. deep_chat appends the hint to the turn's system
prompt; the model still decides, but the nudge points at the right
tool (notably propose_action for do-something prompts).
"""

import re

_IMPLEMENT_VERBS = ("implement", "build", "create", "add", "write",
                    "develop", "code")
_FIX_WORDS = ("fix", "repair", "debug", "broken", "failing", "fails",
              "failed", "fail", "failure", "bug", "error", "crash",
              "regression")
_REVIEW_WORDS = ("review", "pull request")
_INTERROGATIVE = ("how", "what", "why", "when", "where", "which", "who",
                  "explain")
_ACTION_REQUEST = re.compile(
    r"\b(can you|could you|please)\b\s+(\w+)", re.I)


def _words(text):
    return re.findall(r"[a-z0-9#]+", text.lower())


def classify(prompt):
    """implement/fix/review/explain for one turn prompt."""
    text = (prompt or "").strip()
    if not text:
        return "explain"
    words = set(_words(text))
    first = _words(text)[:1]
    # 'Can you <verb> ...' is an action request in question form.
    match = _ACTION_REQUEST.search(text)
    if match:
        verb = match.group(2).lower()
        if verb in ("review",):
            return "review"
        if verb in _FIX_WORDS:
            return "fix"
        if verb in _IMPLEMENT_VERBS:
            return "implement"
    # Bare imperative: leading action verb.
    if first and first[0] in _FIX_WORDS:
        return "fix"
    if first and first[0] in ("review",):
        return "review"
    if first and first[0] in _IMPLEMENT_VERBS:
        return "implement"
    # Information-seeking beats content words ('what does this error
    # mean' explains; 'the page is broken' fixes).
    if "?" in text or words & set(_INTERROGATIVE):
        # ...unless a fix signal dominates a non-question statement.
        if "?" not in text and words & set(_FIX_WORDS) \
                and not words & set(_INTERROGATIVE):
            return "fix"
        return "explain"
    if words & set(_FIX_WORDS):
        return "fix"
    if "pr" in words or "pull request" in text.lower() \
            or "review" in words:
        return "review"
    if words & set(_IMPLEMENT_VERBS):
        return "implement"
    return "explain"


_HINTS = {
    "implement": ("The user is asking for implementation work. "
                  "Investigate read-only with at most a few tool calls, "
                  "then call propose_action(kind='dispatch') with the "
                  "brief — do not write code yourself and do not end "
                  "the turn with only a description."),
    "fix": ("The user reports something broken. Investigate read-only "
            "(code, logs, CI state) first, then propose the repair: "
            "propose_action kind='td' for CI failures, kind='dispatch' "
            "for code changes."),
    "review": ("The user wants a review. Examine the code or PR with "
               "read-only tools and report findings; propose follow-ups "
               "only if asked."),
}


def hint(intent_name):
    """One-line system addition for an intent, else None."""
    return _HINTS.get(intent_name)
