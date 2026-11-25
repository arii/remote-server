#!/usr/bin/env python3
"""Evaluate routing decisions against run outcomes.

Joins dispatch-time task_router decisions (from queue job.json or
ledger.jsonl routing-decision rows) with run outcomes (summary.json or
ledger completion rows) to evaluate empirical model success rates by
tier, rule, and model. Provides the training/evaluation signal for
learned routing.

CLI:
    python3 scripts/eval_routing_outcomes.py --queue-dir /path/to/queue-state
    python3 scripts/eval_routing_outcomes.py --ledger-file /path/to/ledger.jsonl
"""

import argparse
import json
from pathlib import Path
import sys


def load_job_outcomes(queue_dir):
    """Read joined routing decisions and outcomes from queue job directories."""
    records = []
    root = Path(queue_dir)
    if not root.is_dir():
        return records

    for job_path in sorted(root.iterdir()):
        if not job_path.is_dir():
            continue
        job_file = job_path / "job.json"
        summary_file = job_path / "summary.json"
        if not job_file.is_file():
            continue

        try:
            with open(job_file, encoding="utf-8") as handle:
                job_data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue

        issue = job_data.get("issue") or {}
        issue_key = f"{issue.get('repo', 'unknown')}#{issue.get('number', 0)}"
        routing = job_data.get("routing") or {}

        summary_data = {}
        if summary_file.is_file():
            try:
                with open(summary_file, encoding="utf-8") as handle:
                    summary_data = json.load(handle)
            except (OSError, json.JSONDecodeError):
                summary_data = {}

        exit_code = summary_data.get("exit_code")
        has_finished = type(exit_code) is int
        success = exit_code == 0 if has_finished else False

        served_tier = (
            summary_data.get("tier") or job_data.get("model_tier") or "default"
        )
        picks = routing.get("picks") or {}
        pick_info = (
            picks.get(served_tier) or picks.get("strong") or picks.get("fast") or {}
        )
        selected_model = (
            summary_data.get("strong_model") or pick_info.get("model") or "unknown"
        )

        budget_parent = (summary_data.get("budget") or {}).get("parent") or {}
        calls_used = budget_parent.get("calls_used", 0)
        elapsed_s = summary_data.get("elapsed_s", 0.0)

        records.append(
            {
                "issue_key": issue_key,
                "tier_suggested": routing.get("tier", "default"),
                "tier_rule": routing.get("tier_rule", "task:standard"),
                "features": routing.get("features") or {},
                "served_tier": served_tier,
                "selected_model": selected_model,
                "has_finished": has_finished,
                "success": success,
                "exit_code": exit_code,
                "calls_used": calls_used,
                "elapsed_s": elapsed_s,
            }
        )
    return records


def load_ledger_decisions(ledger_file):
    """Read routing decision rows from a ledger.jsonl file."""
    decisions = []
    path = Path(ledger_file)
    if not path.is_file():
        return decisions

    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            extra = row.get("extra") or {}
            if extra.get("event") == "routing-decision":
                decisions.append(
                    {
                        "issue_key": row.get("thread_id")
                        or extra.get("issue", "unknown"),
                        "tier_suggested": extra.get("tier", "default"),
                        "tier_rule": extra.get("tier_rule", "task:standard"),
                        "features": extra.get("features") or {},
                        "requested_tier": extra.get("requested_tier"),
                        "served_tier": extra.get("served_tier"),
                        "picks": extra.get("picks") or {},
                        "timestamp": row.get("started") or row.get("timestamp"),
                    }
                )
    return decisions


def aggregate_routing_metrics(records):
    """Compute aggregate outcomes grouped by (served_tier, model) and tier_rule."""
    by_model = {}
    by_rule = {}

    for rec in records:
        if not rec.get("has_finished"):
            continue

        model_key = f"{rec.get('served_tier', 'default')} | {rec.get('selected_model', 'unknown')}"
        m_stats = by_model.setdefault(
            model_key,
            {"total": 0, "successes": 0, "failures": 0, "calls": 0, "elapsed_s": 0.0},
        )
        m_stats["total"] += 1
        if rec.get("success"):
            m_stats["successes"] += 1
        else:
            m_stats["failures"] += 1
        m_stats["calls"] += rec.get("calls_used", 0)
        m_stats["elapsed_s"] += rec.get("elapsed_s", 0.0)

        rule = rec.get("tier_rule", "task:standard")
        r_stats = by_rule.setdefault(
            rule,
            {"total": 0, "successes": 0, "failures": 0},
        )
        r_stats["total"] += 1
        if rec.get("success"):
            r_stats["successes"] += 1
        else:
            r_stats["failures"] += 1

    summary_by_model = []
    for k, v in sorted(by_model.items()):
        total = v["total"]
        rate = (v["successes"] / total * 100.0) if total else 0.0
        avg_calls = (v["calls"] / total) if total else 0.0
        avg_elapsed = (v["elapsed_s"] / total) if total else 0.0
        summary_by_model.append(
            {
                "tier_and_model": k,
                "total": total,
                "successes": v["successes"],
                "failures": v["failures"],
                "success_rate_pct": round(rate, 1),
                "avg_calls": round(avg_calls, 1),
                "avg_elapsed_s": round(avg_elapsed, 1),
            }
        )

    summary_by_rule = []
    for k, v in sorted(by_rule.items()):
        total = v["total"]
        rate = (v["successes"] / total * 100.0) if total else 0.0
        summary_by_rule.append(
            {
                "rule": k,
                "total": total,
                "successes": v["successes"],
                "failures": v["failures"],
                "success_rate_pct": round(rate, 1),
            }
        )

    return {
        "total_records": len(records),
        "finished_records": sum(1 for r in records if r.get("has_finished")),
        "by_model": summary_by_model,
        "by_rule": summary_by_rule,
    }


def format_report(aggregates):
    """Format aggregated metrics into markdown tables."""
    lines = [
        "# Model Routing Outcome Evaluation",
        "",
        f"Evaluated runs: {aggregates.get('finished_records', 0)} / {aggregates.get('total_records', 0)} total",
        "",
        "## Performance by Tier & Model",
        "| Tier | Model | Runs | Successes | Failures | Success Rate | Avg Calls | Avg Time (s) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in aggregates.get("by_model", []):
        tier, _, model = row["tier_and_model"].partition(" | ")
        lines.append(
            f"| {tier} | {model} | {row['total']} | {row['successes']} | "
            f"{row['failures']} | {row['success_rate_pct']}% | {row['avg_calls']} | {row['avg_elapsed_s']} |"
        )
    if not aggregates.get("by_model"):
        lines.append("| (none) | - | 0 | 0 | 0 | - | - | - |")

    lines.extend(
        [
            "",
            "## Performance by Task Complexity Rule",
            "| Routing Rule | Runs | Successes | Failures | Success Rate |",
            "|---|---|---|---|---|",
        ]
    )
    for row in aggregates.get("by_rule", []):
        lines.append(
            f"| {row['rule']} | {row['total']} | {row['successes']} | "
            f"{row['failures']} | {row['success_rate_pct']}% |"
        )
    if not aggregates.get("by_rule"):
        lines.append("| (none) | 0 | 0 | 0 | - |")

    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queue-dir", help="Path to queue state directory")
    parser.add_argument("--ledger-file", help="Path to ledger.jsonl file")
    parser.add_argument(
        "--json", action="store_true", help="Output JSON instead of markdown"
    )
    args = parser.parse_args(argv)

    if not args.queue_dir and not args.ledger_file:
        parser.error("At least one of --queue-dir or --ledger-file must be provided")

    records = []
    if args.queue_dir:
        records.extend(load_job_outcomes(args.queue_dir))
    if args.ledger_file and not args.queue_dir:
        decisions = load_ledger_decisions(args.ledger_file)
        if args.json:
            print(json.dumps({"decisions": decisions}, indent=2))
            return 0
        print(f"Loaded {len(decisions)} routing decisions from ledger.")
        return 0

    aggregates = aggregate_routing_metrics(records)
    if args.json:
        print(json.dumps({"aggregates": aggregates, "records": records}, indent=2))
    else:
        print(format_report(aggregates))
    return 0


if __name__ == "__main__":
    sys.exit(main())
