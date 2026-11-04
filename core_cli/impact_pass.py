"""Scheduled console UX-regression pass (#869).

One fire per invocation: captures the console's key routes via
``td ux`` (through the deep-agent/impact helpers, reused as-is),
diffs each capture against the stored baseline snapshot, and
ledgers new findings with severity. Rolling baselines: after a
successful pass the fresh capture becomes the next baseline, so
repeat fires stay silent until the UI actually changes.

Findings are data, not failures: the run exits 0 with the count in
stdout even when findings land. Capture errors fail the run (exit
1) so the job_fail push fires.

Invoked by the engine's ``impact-pass`` action step; never imported
by the API (steps are subprocesses, not in-process calls).
"""

from __future__ import annotations

import os
import re
import shutil
import sys

DEFAULT_STATE_DIR = "/var/lib/buttons/impact-pass"
DEFAULT_ROUTES = "/:desktop,/:mobile"
VIEWPORTS = ("desktop", "mobile")


def state_dir() -> str:
    """Snapshot root (env override read dynamically for tests)."""
    return os.environ.get("IMPACT_PASS_STATE_DIR", DEFAULT_STATE_DIR)


def checkout_dir() -> str:
    """Capture target: the console checkout (this repo's root)."""
    override = os.environ.get("IMPACT_PASS_CHECKOUT", "").strip()
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def parse_routes(spec: str) -> list:
    """Parse "route:viewport,..." into [(route, viewport)].

    Raises ValueError on any malformed pair (callers fail the run
    so a misconfig pages instead of silently passing).
    """
    pairs = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        route, sep, viewport = chunk.rpartition(":")
        if not sep or not route or viewport not in VIEWPORTS:
            raise ValueError(f"bad route spec: {chunk!r} "
                             f"(want route:desktop|mobile)")
        pairs.append((route, viewport))
    if not pairs:
        raise ValueError("no routes configured")
    return pairs


def route_key(route: str, viewport: str) -> str:
    """Filesystem-safe snapshot key for one route+viewport."""
    slug = re.sub(r"[^a-z0-9]+", "-", route.strip("/").lower())
    slug = slug.strip("-") or "root"
    return f"{slug}-{viewport}"


def _load_impact():
    """deep-agent/impact on sys.path (review.py pattern)."""
    here = os.path.dirname(os.path.abspath(__file__))
    deep_agent = os.path.join(os.path.dirname(here), "deep-agent")
    if deep_agent not in sys.path:
        sys.path.insert(0, deep_agent)
    import impact

    return impact


def run_pass(routes, checkout, root, ledger_append) -> int:
    """Capture, diff, and ledger one pass. Returns the exit code."""
    impact = _load_impact()
    total_findings = 0
    seeded = 0
    for route, viewport in routes:
        if not impact.valid_route(route):
            print(f"error [{route} {viewport}] bad route", flush=True)
            return 1
        try:
            res = impact.run_ux_capture(
                checkout, route=route,
                desktop=(viewport == "desktop"),
                mobile=(viewport == "mobile"))
        except ValueError as exc:
            print(f"error [{route} {viewport}] {exc}", flush=True)
            return 1
        if not res.get("ok"):
            print(f"error [{route} {viewport}] "
                  f"{res.get('error', 'capture failed')}", flush=True)
            return 1
        src = (res.get("artifacts") or {}).get("base", "")
        if not src or not os.path.isdir(src):
            print(f"error [{route} {viewport}] capture ok but no "
                  f"artifacts at {src!r}", flush=True)
            return 1
        key = route_key(route, viewport)
        key_dir = os.path.join(root, key)
        os.makedirs(key_dir, exist_ok=True)
        current = os.path.join(key_dir, "current")
        baseline = os.path.join(key_dir, "baseline")
        if os.path.exists(current):
            shutil.rmtree(current)
        shutil.copytree(src, current)
        if not os.path.isdir(baseline):
            os.rename(current, baseline)
            seeded += 1
            print(f"seeded [{route} {viewport}] baseline {baseline}",
                  flush=True)
            continue
        diff = impact.diff_versions(baseline, current)
        findings = diff.get("findings", [])
        for hit in findings:
            ledger_append({
                "kind": "ux-finding",
                "source": "impact-pass",
                "severity": hit.get("severity", "info"),
                "title": hit.get("title", ""),
                "route": hit.get("route", route),
                "viewport": hit.get("viewport", viewport),
                "evidence": str(hit.get("evidence", ""))[:500],
            })
        shutil.rmtree(baseline)
        os.rename(current, baseline)
        total_findings += len(findings)
        print(f"pass [{route} {viewport}] "
              f"findings={len(findings)} "
              f"compared={diff.get('compared', 0)}", flush=True)
    print(f"impact-pass routes={len(routes)} "
          f"findings={total_findings} seeded={seeded}", flush=True)
    return 0


def main(argv=None) -> int:
    try:
        routes = parse_routes(
            os.environ.get("IMPACT_PASS_ROUTES", DEFAULT_ROUTES))
    except ValueError as exc:
        print(f"impact-pass config error: {exc}", flush=True)
        return 1

    from backend.ledger import append as ledger_append

    return run_pass(routes, checkout_dir(), state_dir(), ledger_append)


if __name__ == "__main__":
    sys.exit(main())
