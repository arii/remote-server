"""Render a deep-agent run trace as HTML timeline or phone text.

Input: a trace JSONL file (observe.py shapes) or a run_id / thread
resolvable via runs.py. Stdlib-only: works under system python3.

Usage:
  python3 trace_view.py <tracefile|run-id> [--html out.html] [--text]

  no flags   -> text summary on stdout.
  --text     -> text summary on stdout (explicit).
  --html OUT -> single-file self-contained HTML timeline (inline CSS,
                no JS at all) written to OUT.

Malformed lines never raise: they are skipped and the count is noted
in both outputs. Unknown event types render as "?".
"""

import argparse
import datetime
import html
import json
import os
import sys

import cli

cli.bootstrap()

import runs  # noqa: E402

TEXT_WIDTH = 72
TRUNC = "…"


def _cap(text, width):
    text = str(text).replace("\n", " ").replace("\r", " ")
    if len(text) > width:
        return text[:max(0, width - 1)] + TRUNC
    return text


def parse_trace(path):
    """(events, malformed): dicts with a string type; blanks skipped."""
    events = []
    malformed = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(obj, dict) or not isinstance(
                    obj.get("type"), str):
                malformed += 1
                continue
            events.append(obj)
    return events, malformed


def summarize(events):
    """Aggregate stats over parsed events. Never raises."""
    stats = {"n_events": len(events), "models": 0,
             "tools_ok": 0, "tools_err": 0,
             "in_tokens": 0, "out_tokens": 0, "tot_tokens": 0,
             "duration_s": None, "run_ok": None}
    try:
        for e in events:
            t = e.get("type")
            if t == "model_request":
                stats["models"] += 1
            elif t == "model_response":
                u = e.get("usage") or {}
                if isinstance(u, dict):
                    stats["in_tokens"] += u.get("input_tokens", 0) or 0
                    stats["out_tokens"] += u.get("output_tokens", 0) or 0
                    stats["tot_tokens"] += u.get("total_tokens", 0) or 0
            elif t == "tool_call":
                if e.get("ok"):
                    stats["tools_ok"] += 1
                else:
                    stats["tools_err"] += 1
            elif t == "run_end":
                stats["duration_s"] = e.get("duration_s")
                stats["run_ok"] = e.get("ok")
        if stats["duration_s"] is None:
            ts = [e.get("ts") for e in events
                  if isinstance(e.get("ts"), (int, float))]
            if len(ts) >= 2:
                stats["duration_s"] = round(max(ts) - min(ts), 3)
    except (TypeError, ValueError, AttributeError):
        pass
    return stats


def _offset(e, t0):
    ts = e.get("ts")
    if isinstance(ts, (int, float)) and isinstance(t0, (int, float)):
        return f"+{max(0.0, ts - t0):.1f}s"
    return "+?s"


def render_text(events, malformed, label):
    """One line per event, every line <= TEXT_WIDTH chars."""
    t0 = next((e.get("ts") for e in events
               if isinstance(e.get("ts"), (int, float))), None)
    lines = [_cap(f"trace {label} ({len(events)} events)",
                TEXT_WIDTH)]
    for e in events:
        t = e.get("type")
        off = _offset(e, t0)
        if t == "run_start":
            line = (f"{off} START {e.get('script', '?')} "
                    f"thread={e.get('thread', '?')} "
                    f"model={e.get('model', '?')}")
        elif t == "model_request":
            line = f"{off} LLM req n_msgs={e.get('n_messages', '?')}"
        elif t == "model_response":
            u = e.get("usage") or {}
            if not isinstance(u, dict):
                u = {}
            line = (f"{off} LLM resp in={u.get('input_tokens', 0)} "
                    f"out={u.get('output_tokens', 0)} "
                    f"tot={u.get('total_tokens', 0)}")
        elif t == "tool_call":
            dur = e.get("duration_s", "?")
            mark = "ok" if e.get("ok") else "ERR"
            args = _cap(e.get("args_preview", ""), 28)
            if e.get("ok"):
                res = _cap(e.get("result_preview", ""), 28)
                line = (f"{off} {mark} {e.get('name', '?')} {dur}s "
                        f"{args} -> {res}")
            else:
                err = _cap(e.get("error", ""), 30)
                line = (f"{off} {mark} {e.get('name', '?')} {dur}s "
                        f"err={err}")
        elif t == "run_end":
            line = (f"{off} END ok={e.get('ok')} "
                    f"dur={e.get('duration_s', '?')}s")
        else:
            line = f"{off} ? {t}"
        lines.append(_cap(line, TEXT_WIDTH))
    s = summarize(events)
    lines.append(_cap(
        f"tokens in={s['in_tokens']} out={s['out_tokens']} "
        f"tot={s['tot_tokens']} | tools "
        f"ok={s['tools_ok']} err={s['tools_err']} | "
        f"dur={s['duration_s']}s", TEXT_WIDTH))
    if malformed:
        lines.append(f"skipped {malformed} malformed "
                     f"line{'s' if malformed != 1 else ''}")
    return "\n".join(lines) + "\n"


CSS = """
*{box-sizing:border-box}
body{font:15px/1.45 -apple-system,system-ui,sans-serif;margin:0 auto;
  max-width:44rem;padding:.75rem;color:#111;background:#fff}
h1{font-size:1.05rem;word-break:break-word;margin:.2rem 0 .6rem}
.chips{display:flex;flex-wrap:wrap;gap:.35rem;margin-bottom:.8rem}
.chip{background:#f1f3f5;border-radius:1rem;padding:.1rem .6rem;
  font-size:.8rem;white-space:nowrap}
.chip.bad{background:#ffe3e3}.chip.good{background:#d3f9d8}
ol.tl{list-style:none;margin:0;padding:0}
ol.tl li{border-left:3px solid #adb5bd;margin:0 0 .55rem .4rem;
  padding:.35rem .6rem;background:#f8f9fa;border-radius:0 .4rem .4rem 0;
  overflow-wrap:anywhere}
li.model{border-color:#4dabf7}li.tool-ok{border-color:#40c057}
li.tool-err{border-color:#fa5252}li.bound{border-color:#868e96;
  background:#e9ecef}
.row1{display:flex;flex-wrap:wrap;gap:.4rem;align-items:baseline}
.t{color:#868e96;font-size:.78rem;font-variant-numeric:tabular-nums}
.badge{font-weight:700;font-size:.82rem}
.name{font-family:ui-monospace,monospace;font-size:.85rem;
  overflow-wrap:anywhere}
.dur{font-size:.8rem;color:#495057}
details{font-size:.8rem;color:#343a40;margin-top:.2rem}
summary{cursor:pointer;color:#1971c2}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#fff;
  border:1px solid #dee2e6;border-radius:.3rem;padding:.4rem;
  font-size:.75rem;margin:.3rem 0 0;max-height:12rem;overflow:auto}
.skip{background:#fff3bf;border-radius:.4rem;padding:.4rem .6rem;
  font-size:.85rem;margin-top:.6rem}
@media (prefers-color-scheme:dark){
  body{color:#e9ecef;background:#121417}
  ol.tl li{background:#1c1f24}li.bound{background:#262b31}
  .chip{background:#262b31}.chip.bad{background:#5c2b2b}
  .chip.good{background:#2b4a2e}pre{background:#121417;
    border-color:#343a40;color:#e9ecef}.t{color:#9099a3}
  summary{color:#74c0fc}.skip{background:#4a3f1d}}
""".strip()


def _iso(ts):
    if isinstance(ts, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).strftime("%H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return "?"
    return "?"


def render_html(events, malformed, label):
    """Single-file HTML timeline: inline CSS, zero JS."""
    s = summarize(events)
    t0 = next((e.get("ts") for e in events
               if isinstance(e.get("ts"), (int, float))), None)
    items = []
    for e in events:
        t = e.get("type")
        off = _offset(e, t0)
        when = _iso(e.get("ts"))
        if t == "run_start":
            cls, badge = "bound", "START"
            body = (f"<span class='name'>{html.escape(str(e.get('script', '?')))}"
                    f"</span> thread={html.escape(str(e.get('thread', '?')))} "
                    f"model={html.escape(str(e.get('model', '?')))}")
        elif t == "model_request":
            cls, badge = "model", "LLM req"
            body = f"n_msgs={html.escape(str(e.get('n_messages', '?')))}"
        elif t == "model_response":
            u = e.get("usage") or {}
            if not isinstance(u, dict):
                u = {}
            cls, badge = "model", "LLM resp"
            body = (f"in={u.get('input_tokens', 0)} "
                    f"out={u.get('output_tokens', 0)} "
                    f"tot={u.get('total_tokens', 0)}")
        elif t == "tool_call":
            ok = bool(e.get("ok"))
            cls = "tool-ok" if ok else "tool-err"
            badge = "ok" if ok else "ERR"
            det = _preview_block(e, ok)
            body = (f"<span class='name'>{html.escape(str(e.get('name', '?')))}"
                    f"</span> <span class='dur'>{e.get('duration_s', '?')}s"
                    f"</span>{det}")
        elif t == "run_end":
            cls, badge = "bound", "END"
            body = (f"ok={e.get('ok')} "
                    f"dur={e.get('duration_s', '?')}s")
        else:
            cls, badge = "", "?"
            body = html.escape(str(t))
        items.append(
            f"<li class='{cls}'><div class='row1'>"
            f"<span class='t'>{off} ({when})</span>"
            f"<span class='badge'>{badge}</span><span>{body}</span>"
            f"</div></li>")
    chips = [
        f"{s['n_events']} events",
        f"dur={s['duration_s']}s",
        f"tok in={s['in_tokens']} out={s['out_tokens']} "
        f"tot={s['tot_tokens']}",
        f"models={s['models']}",
        f"tools ok={s['tools_ok']} err={s['tools_err']}",
    ]
    chips_html = "".join(f"<span class='chip'>{html.escape(c)}</span>"
                         for c in chips)
    if s["tools_err"]:
        chips_html += "<span class='chip bad'>has errors</span>"
    skip = ""
    if malformed:
        plural = "s" if malformed != 1 else ""
        skip = (f"<div class='skip'>skipped {malformed} malformed "
                f"line{plural}</div>")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>trace {html.escape(label)}</title>
<style>
{CSS}
</style>
</head>
<body>
<h1>trace {html.escape(label)}</h1>
<div class="chips">{chips_html}</div>
<ol class="tl">
{"".join(items)}
</ol>
{skip}
</body>
</html>
"""


def _preview_block(e, ok):
    args = _cap(e.get("args_preview", ""), 2000)
    if ok:
        res = _cap(e.get("result_preview", ""), 2000)
        inner = (f"args: {html.escape(args)}\n"
                 f"result: {html.escape(res)}")
    else:
        inner = (f"args: {html.escape(args)}\n"
                 f"error: {html.escape(str(e.get('error', ''))[:500])}")
    return f"<details><summary>detail</summary><pre>{inner}</pre></details>"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a deep-agent run trace.")
    parser.add_argument("ref", help="trace JSONL file, run_id, or thread")
    parser.add_argument("--html", metavar="OUT",
                        help="write self-contained HTML timeline to OUT")
    parser.add_argument("--text", action="store_true",
                        help="print plain-text summary")
    args = parser.parse_args(argv)
    path = runs.resolve_run(args.ref)
    if path is None or not os.path.isfile(path):
        # Stdout, like every Sorry line: exit 1 promises the reply
        # on stdout so chunk readers show it on the phone.
        return cli.sorry(f"Sorry, no trace found for {args.ref!r}.")
    try:
        events, malformed = parse_trace(path)
    except OSError as exc:
        return cli.sorry(f"Sorry, cannot read trace {path}: {exc}.")
    label = os.path.basename(path)
    if label.endswith(".jsonl"):
        label = label[:-len(".jsonl")]
    if args.html:
        try:
            with open(args.html, "w", encoding="utf-8") as f:
                f.write(render_html(events, malformed, label))
        except OSError as exc:
            return cli.sorry(f"Sorry, cannot write {args.html}: {exc}.")
        size = os.path.getsize(args.html)
        print(f"wrote {args.html} ({size} bytes, {len(events)} events)")
    if args.text or not args.html:
        sys.stdout.write(render_text(events, malformed, label))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())