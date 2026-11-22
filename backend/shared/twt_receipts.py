"""Durable per-request TwT receipts and bounded pipeline shutdown settlement."""

import contextvars
import fcntl
import json
import math
import os
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from backend.shared import ledger
from backend.shared import twt_transport

current = contextvars.ContextVar("twt_run", default=None)
latest = contextvars.ContextVar("twt_request", default=None)


def capture(request):
    scope = current.get()
    if scope is None or not request.url.path.endswith(
        ("/chat/completions", "/api/chat")
    ):
        return
    trace_id = scope.request(local_only=request.url.path.endswith("/local/api/chat"))
    request.headers["X-Trace-Id"] = trace_id
    request.headers["X-Session-Id"] = scope.run_id
    latest.set(trace_id)


async def acapture(request):
    capture(request)


def tag(result):
    trace_id = latest.get()
    if trace_id:
        for generation in result.generations:
            generation.message.response_metadata["headers"] = {
                **(generation.generation_info or {}).get("headers", {}),
                **generation.message.response_metadata.get("headers", {}),
                "x-trace-id": trace_id,
            }
    return result


def fetch(base_url, ids, *, uds=None):
    if uds or twt_transport.uds_path():
        import httpx

        with httpx.Client(**twt_transport.client_kwargs(uds=uds), timeout=10) as client:
            response = client.post(
                base_url.rstrip("/") + "/receipts",
                json={"trace_ids": ids, "cancel": True},
            )
            response.raise_for_status()
            return response.json()["receipts"]
    request = urllib.request.Request(
        base_url.rstrip("/") + "/receipts",
        data=json.dumps({"trace_ids": ids, "cancel": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["receipts"]


def _owns_receipts(row):
    if not isinstance(row, dict):
        return False
    extra, usage = row.get("extra", {}), row.get("usage", {})
    if not isinstance(extra, dict) or not isinstance(usage, dict):
        return False
    if extra.get("phase") == "summary" or extra.get("llm_call") is False:
        return False
    trace_ids = usage.get("twt_trace_ids", [])
    if not isinstance(trace_ids, list) or any(
        not isinstance(value, str) for value in trace_ids
    ):
        return False
    llm_call = extra.get("llm_call")
    return llm_call is not False and (llm_call is True or row.get("metered") is True)


def merge_receipts(ids, receipts, run_id, started, *, local_ids=()):
    """Refresh only identified rows; add missing receipts, never a cost delta."""
    path = Path(ledger.LEDGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ledger.locked(path):
        return _merge_receipts_locked(
            path, ids, receipts, run_id, started, set(local_ids)
        )


def _known_cost(row):
    cost = row.get("cost_usd")
    return type(cost) in (int, float) and math.isfinite(cost) and cost >= 0


def receipt_cost(receipt):
    """Budget charge: authoritative cost or an explicitly bounded reservation."""
    if not isinstance(receipt, dict):
        return None
    if receipt.get("cost_complete") is True:
        return receipt["cost_usd"] if _known_cost(receipt) else None
    bound = receipt.get("cost_upper_bound_usd")
    if (
        receipt.get("cost_basis") == "upper_bound"
        and type(bound) in (int, float)
        and math.isfinite(bound)
        and bound >= 0
    ):
        return bound
    return None


def _settled_rows():
    path = Path(ledger.LEDGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    settled = []
    with ledger.locked(path):
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if (
                        _owns_receipts(row)
                        and row.get("extra", {}).get("twt_receipt_reconciled") is True
                        and _known_cost(row)
                        and row.get("cost_basis") != "upper_bound"
                    ):
                        settled.append(row)
        except FileNotFoundError:
            pass
    return settled


def _settled_trace_ids():
    return {
        trace
        for row in _settled_rows()
        for trace in row["usage"].get("twt_trace_ids", [])
    }


def ledger_receipts(ids, receipts):
    """Promote snapshots using authoritative, already-reconciled actual charges."""
    promoted = dict(receipts)
    for row in _settled_rows():
        traces = set(row["usage"].get("twt_trace_ids", []))
        costs = row["usage"].get("twt_receipt_costs", {})
        if len(traces) == 1:
            costs = {next(iter(traces)): row["cost_usd"]}
        if (
            not isinstance(costs, dict)
            or set(costs) != traces
            or not all(_known_cost({"cost_usd": cost}) for cost in costs.values())
            or not math.isclose(
                sum(costs.values()), row["cost_usd"], rel_tol=0, abs_tol=1e-9
            )
        ):
            continue
        for trace in traces.intersection(ids):
            old = promoted.get(trace, {})
            if (
                old.get("cost_complete") is True
                and receipt_cost(old) == costs[trace]
                and old.get("cost_basis") != "upper_bound"
            ):
                continue
            actual = {
                **old,
                "trace_id": trace,
                "cost_complete": True,
                "cost_usd": costs[trace],
                "cost_source": "ledger",
            }
            for key in (
                "cost_basis",
                "cost_upper_bound_usd",
                "prompt_tokens_upper_bound",
                "completion_tokens_upper_bound",
            ):
                actual.pop(key, None)
            promoted[trace] = actual
    return promoted


def _merge_receipts_locked(path, ids, receipts, run_id, started, local_ids):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        rows = []
        for line in handle:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                row = None
            rows.append(row if isinstance(row, dict) else line)
        represented = set()
        for row in rows:
            if not _owns_receipts(row):
                continue
            trace_ids = set(row.get("usage", {}).get("twt_trace_ids", []))
            if not trace_ids.intersection(ids):
                continue
            if not trace_ids.issubset(ids) or represented.intersection(trace_ids):
                raise ValueError("TwT receipt IDs overlap ledger rows or runs")
            represented.update(trace_ids)
        for trace_id in sorted(set(ids) - represented):
            rows.append(
                {
                    "kind": "deep-agent-run",
                    "ts": time.time(),
                    "script": "twt_receipts.py",
                    "model": "default",
                    "provider": "ollama" if trace_id in local_ids else "twt",
                    "thread_id": run_id,
                    "duration_s": round(time.time() - started, 1),
                    "tool_calls": [],
                    "tool_results": [],
                    "usage": {"twt_trace_ids": [trace_id]},
                    "cost_usd": None,
                    "metered": trace_id not in local_ids,
                    "final_preview": "",
                    "extra": {
                        "llm_call": True,
                        "error": "response not delivered before pipeline shutdown",
                    },
                }
            )
        missing = set()
        for row in rows:
            if not _owns_receipts(row):
                continue
            trace_ids = set(row.get("usage", {}).get("twt_trace_ids", []))
            if not trace_ids or not trace_ids.issubset(ids):
                continue
            if (
                row.get("extra", {}).get("twt_receipt_reconciled") is True
                and _known_cost(row)
                and row.get("cost_basis") != "upper_bound"
            ):
                continue
            missing_local = (trace_ids & local_ids) - receipts.keys()
            found = [
                {"cost_complete": True, "cost_usd": 0.0}
                if trace_id in missing_local
                else receipts.get(trace_id)
                for trace_id in trace_ids
            ]
            if missing_local:
                row.setdefault("extra", {})["twt_local_receipt_missing"] = sorted(
                    missing_local
                )
            else:
                row.setdefault("extra", {}).pop("twt_local_receipt_missing", None)
            charges = [receipt_cost(receipt) for receipt in found]
            complete = all(charge is not None for charge in charges)
            if (
                not complete
                and row.get("cost_basis") == "upper_bound"
                and _known_cost(row)
            ):
                continue
            row.setdefault("extra", {})["twt_receipt_reconciled"] = complete
            if not complete:
                if not _known_cost(row):
                    row["usage"].pop("reported_cost_usd", None)
                    row["cost_usd"] = None
                missing.update(trace_ids)
                continue
            cost = sum(charges)
            reserved = any(
                receipt.get("cost_complete") is not True for receipt in found
            )
            if trace_ids.issubset(local_ids):
                if cost != 0:
                    raise ValueError("Local TwT receipt reported paid cost")
                row.update(provider="ollama", metered=False)
            prompt = sum(receipt.get("prompt_tokens") or 0 for receipt in found)
            completion = sum(receipt.get("completion_tokens") or 0 for receipt in found)
            if not missing_local and not reserved:
                row["usage"].update(
                    input_tokens=prompt,
                    output_tokens=completion,
                    total_tokens=prompt + completion,
                )
            if reserved:
                row["cost_basis"] = "upper_bound"
                row["cost_upper_bound_usd"] = cost
                row["usage"].pop("reported_cost_usd", None)
                row["usage"].pop("twt_receipt_costs", None)
            else:
                row.pop("cost_basis", None)
                row.pop("cost_upper_bound_usd", None)
                row["usage"]["reported_cost_usd"] = cost
                row["usage"]["twt_receipt_costs"] = dict(zip(trace_ids, charges))
            row["cost_usd"] = cost
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix="." + path.name + ".",
                delete=False,
            ) as out:
                temporary = out.name
                for row in rows:
                    if isinstance(row, dict):
                        out.write(json.dumps(row) + "\n")
                    else:
                        out.write(row if row.endswith("\n") else row + "\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
    return sorted(missing)


class ReceiptRun:
    def __init__(self, run_id, base_url=None):
        self.run_id = run_id
        self.base_url = base_url or os.environ.get(
            "TWT_BASE_URL", "http://127.0.0.1:8000/v1"
        )
        if urlsplit(self.base_url).username or urlsplit(self.base_url).password:
            raise ValueError("TwT URL must not contain credentials")
        self.started = time.time()
        self.uds = twt_transport.uds_path()
        self.ids = set()
        self.local_ids = set()
        self.lock = threading.Lock()
        self.stopped = False
        self.settled = False
        self.error = None
        self.path = (
            Path(ledger.state_dir()) / "receipts" / (str(uuid.uuid4()) + ".jsonl")
        )

    def request(self, *, local_only=False):
        with self.lock:
            if self.stopped:
                raise RuntimeError("Pipeline stopped; refusing a new TwT request")
            trace_id = str(uuid.uuid4())
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "trace_id": trace_id,
                            "run_id": self.run_id,
                            "base_url": self.base_url,
                            "started": self.started,
                            "ledger_path": ledger.LEDGER,
                            "local_only": local_only,
                            "uds": self.uds,
                        }
                    )
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            self.ids.add(trace_id)
            if local_only:
                self.local_ids.add(trace_id)
            return trace_id

    def reconcile(
        self, fetch_fn=None, *, timeout=200, clock=time.monotonic, sleep=time.sleep
    ):
        if fetch_fn is None:

            def fetch_fn(base, ids):
                return fetch(base, ids, uds=self.uds)

        with self.lock:
            if self.settled:
                return
            self.stopped = True
            ids = sorted(self.ids)
        if not ids:
            self.settled = True
            return
        deadline = clock() + timeout
        receipts = {}
        settlement = self.path.with_suffix(".settlement.json")
        if settlement.exists():
            for receipt in json.loads(settlement.read_text()).get("receipts", []):
                trace_id = receipt.get("trace_id")
                if trace_id not in self.ids or trace_id in receipts:
                    raise ValueError("Unexpected or duplicate saved TwT receipt ID")
                receipts[trace_id] = receipt
        receipts = ledger_receipts(set(ids), receipts)
        failure = None
        settled = _settled_trace_ids()
        pending = set(ids) - settled
        pending.update(
            trace
            for trace in set(ids) & settled
            if receipts.get(trace, {}).get("cost_complete") is not True
            or receipt_cost(receipts.get(trace)) is None
        )
        pending -= {
            trace_id
            for trace_id, receipt in receipts.items()
            if receipt.get("cost_complete") is True
            and receipt_cost(receipt) is not None
        }
        while pending:
            try:
                for receipt in fetch_fn(self.base_url, sorted(pending)):
                    if receipt.get("trace_id") not in self.ids:
                        raise ValueError("Unexpected TwT receipt ID")
                    if (
                        receipt_cost(receipt) is not None
                        or receipt["trace_id"] not in receipts
                    ):
                        receipts[receipt["trace_id"]] = receipt
            except Exception as error:
                failure = type(error).__name__
                break
            if (
                all(
                    trace_id in receipts or trace_id in self.local_ids
                    for trace_id in pending
                )
                or clock() >= deadline
            ):
                break
            pending -= {
                trace_id
                for trace_id, receipt in receipts.items()
                if receipt_cost(receipt) is not None
            }
            sleep(min(1, max(0, deadline - clock())))
        try:
            missing = merge_receipts(
                set(ids), receipts, self.run_id, self.started, local_ids=self.local_ids
            )
        except Exception as error:
            missing = sorted(ids)
            failure = failure or str(error)
        self.error = (failure or "unsettled TwT receipts") if missing else None
        temporary = settlement.with_suffix(settlement.suffix + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(
                {
                    "trace_ids": ids,
                    "local_trace_ids": sorted(self.local_ids),
                    "missing": missing,
                    "error": self.error,
                    "receipts": list(receipts.values()),
                },
                handle,
                indent=2,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, settlement)
        if self.error:
            print(f"TwT receipt settlement incomplete: {self.error}", file=sys.stderr)
        else:
            self.settled = True


@contextmanager
def run(run_id):
    scope = ReceiptRun(run_id)
    token = current.set(scope)
    try:
        yield scope
    finally:
        try:
            scope.reconcile()
        finally:
            current.reset(token)


def main():
    entries = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines()]
    ledger.LEDGER = entries[0]["ledger_path"]
    scope = ReceiptRun(entries[0]["run_id"], entries[0]["base_url"])
    scope.path = Path(sys.argv[1])
    scope.started = entries[0]["started"]
    scope.uds = entries[0].get("uds")
    scope.ids = {entry["trace_id"] for entry in entries}
    scope.local_ids = {
        entry["trace_id"] for entry in entries if entry.get("local_only") is True
    }
    scope.reconcile()
    return int(bool(scope.error))


if __name__ == "__main__":
    raise SystemExit(main())
