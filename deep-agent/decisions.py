"""Per-turn decision summaries: what the turn did + why (issue #414).

One line per decision, summaries over transcripts. deep_chat builds
these at record time from in-hand data (tool calls + results, turn
intent/mode, HITL verdict/message); runs flow them to run detail via
extra.decisions. Stdlib-only, never raises: observability must not
break runs.
"""

WHAT_LIMIT = 240
WHY_LIMIT = 240
ARGS_LIMIT = 120


def _first_line(text, limit):
    """First non-blank line of text, capped. '' when none."""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""


def tool_text(content):
    """Readable text from a ToolMessage content: plain str, content
    blocks, or anything else. Never raises."""
    try:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [p.get("text", "") if isinstance(p, dict)
                     else str(p) for p in content]
            return " ".join(p for p in parts if p)
        if content is None:
            return ""
        return str(content)
    except Exception:
        return ""


def _verdict_word(verdict):
    if verdict == "approve":
        return "approved"
    if verdict == "reject":
        return "rejected"
    return f"decided {verdict}"


def build_decisions(calls, results, reply, verdict=None, message=None,
                    error=None, proposals=None, reason=None):
    """Decision lines [{what, why}] for one turn record.

    calls: [{name, args}] in run order; results: [result text]
    aligned by position; reply: final text (answer-line fallback
    only); verdict/message: HITL resume context; error: failure
    text; proposals: [proposal text] for HITL context; reason:
    human phrase for the answer line's why (turn intent/mode).
    Never raises.
    """
    try:
        return _build(calls, results, reply, verdict, message,
                      error, proposals, reason)
    except Exception:
        return []


def _build(calls, results, reply, verdict, message, error, proposals,
           reason):
    lines = []
    calls = [c for c in (calls or [])
             if isinstance(c, dict) and c.get("name")]
    results = list(results or [])
    for i, call in enumerate(calls):
        args = _first_line(call.get("args", ""), ARGS_LIMIT)
        what = f"ran {call['name']}" + (f"({args})" if args else "")
        res = _first_line(tool_text(results[i])
                          if i < len(results) else "", WHY_LIMIT)
        lines.append({"what": what[:WHAT_LIMIT],
                      "why": (f"\u2192 {res}" if res
                              else "\u2192 (no output recorded)")})
    if verdict:
        why = _first_line(message or "", WHY_LIMIT)
        if not why:
            for p in proposals or []:
                why = _first_line(p, WHY_LIMIT)
                if why:
                    break
        lines.append({"what": f"human {_verdict_word(verdict)}"
                              " the proposal",
                      "why": why or "(no reason given)"})
    if error:
        lines.append({"what": "turn failed",
                      "why": _first_line(error, WHY_LIMIT)
                      or "(no error recorded)"})
        return lines
    answer = (_first_line(reason, WHY_LIMIT) if reason
              else _first_line(reply, WHY_LIMIT))
    n = len(calls)
    what = (f"answered from {n} tool result"
            f"{'s' if n != 1 else ''}" if n
            else "answered directly (no tools)")
    lines.append({"what": what, "why": answer or "(empty reply)"})
    return lines
