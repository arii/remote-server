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

import ledger

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


def fetch(base_url, ids):
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
            complete = all(
                receipt is not None
                and receipt.get("cost_complete") is True
                and type(receipt.get("cost_usd")) in (int, float)
                and math.isfinite(receipt["cost_usd"])
                and receipt["cost_usd"] >= 0
                for receipt in found
            )
            row.setdefault("extra", {})["twt_receipt_reconciled"] = complete
            row["usage"].pop("reported_cost_usd", None)
            row["cost_usd"] = None
            if not complete:
                missing.update(trace_ids)
                continue
            cost = sum(receipt["cost_usd"] for receipt in found)
            if trace_ids.issubset(local_ids):
                if cost != 0:
                    raise ValueError("Local TwT receipt reported paid cost")
                row.update(provider="ollama", metered=False)
            prompt = sum(receipt.get("prompt_tokens") or 0 for receipt in found)
            completion = sum(receipt.get("completion_tokens") or 0 for receipt in found)
            if not missing_local:
                row["usage"].update(
                    input_tokens=prompt,
                    output_tokens=completion,
                    total_tokens=prompt + completion,
                )
            row["usage"]["reported_cost_usd"] = cost
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
        self, fetch_fn=fetch, *, timeout=200, clock=time.monotonic, sleep=time.sleep
    ):
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
        failure = None
        while True:
            try:
                for receipt in fetch_fn(self.base_url, ids):
                    if receipt.get("trace_id") not in self.ids:
                        raise ValueError("Unexpected TwT receipt ID")
                    receipts[receipt["trace_id"]] = receipt
            except Exception as error:
                failure = type(error).__name__
                break
            if (
                all(
                    trace_id in receipts or trace_id in self.local_ids
                    for trace_id in ids
                )
                or clock() >= deadline
            ):
                break
            sleep(min(1, max(0, deadline - clock())))
        missing = merge_receipts(
            set(ids), receipts, self.run_id, self.started, local_ids=self.local_ids
        )
        self.error = (failure or "unsettled TwT receipts") if missing else None
        self.path.with_suffix(".settlement.json").write_text(
            json.dumps(
                {
                    "trace_ids": ids,
                    "local_trace_ids": sorted(self.local_ids),
                    "missing": missing,
                    "error": self.error,
                    "receipts": list(receipts.values()),
                },
                indent=2,
            )
        )
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
    scope.ids = {entry["trace_id"] for entry in entries}
    scope.local_ids = {
        entry["trace_id"] for entry in entries if entry.get("local_only") is True
    }
    scope.reconcile()
    return int(bool(scope.error))


if __name__ == "__main__":
    raise SystemExit(main())
