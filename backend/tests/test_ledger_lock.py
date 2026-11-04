"""ledger.read() locking regression tests (#456).

append()'s prune path and clear() rewrite the ledger file under
ledger_lock, but reads slurped it lock-free: a read landing
mid-rewrite saw a torn file and silently returned incomplete
history. These tests pin the fix: the console slurp holds
ledger_lock, and concurrent prune/read traffic yields whole lines
only.
"""

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from backend import ledger


class ReadLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_path = os.environ.get("BUTTONS_LEDGER_PATH")
        self._old_eval = os.environ.get("EVAL_LEDGER")
        self._old_state = os.environ.get("DEEP_AGENT_STATE_DIR")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(
            self.tmp, "ledger.jsonl")
        # Isolate the CLI merge source: an empty file contributes
        # nothing, so read() reflects the console ledger only.
        cli_path = os.path.join(self.tmp, "cli-ledger.jsonl")
        open(cli_path, "w").close()
        os.environ["EVAL_LEDGER"] = cli_path
        os.environ.pop("DEEP_AGENT_STATE_DIR", None)

    def tearDown(self):
        for key, old in (("BUTTONS_LEDGER_PATH", self._old_path),
                         ("EVAL_LEDGER", self._old_eval),
                         ("DEEP_AGENT_STATE_DIR", self._old_state)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    def test_read_blocks_until_rewrite_completes(self):
        """A read racing a locked rewrite waits for the full file.

        Fails before the fix: the lockless slurp returns at once
        with the torn line skipped ([]), so the reader thread is
        already done when we check.
        """
        path = os.environ["BUTTONS_LEDGER_PATH"]
        ledger.append({"kind": "run", "seq": 1})
        writer_ready = threading.Event()
        finish = threading.Event()

        def writer():
            with ledger.ledger_lock:
                with open(path, "w") as f:
                    f.write('{"kind": "run", "seq": 1\n')
                    f.flush()
                writer_ready.set()
                self.assertTrue(finish.wait(timeout=10))
                with open(path, "w") as f:
                    f.write(json.dumps(
                        {"kind": "run", "seq": 42, "ts": 1}) + "\n")

        t = threading.Thread(target=writer)
        t.start()
        self.assertTrue(writer_ready.wait(timeout=10))
        try:
            result = {}
            rt = threading.Thread(
                target=lambda: result.update(entries=ledger.read()))
            rt.start()
            rt.join(timeout=0.5)
            self.assertTrue(
                rt.is_alive(),
                "read() returned mid-rewrite: torn snapshot visible")
            finish.set()
            t.join(timeout=10)
            rt.join(timeout=10)
            self.assertFalse(rt.is_alive())
            self.assertEqual(
                [e.get("seq") for e in result["entries"]], [42])
        finally:
            finish.set()
            t.join(timeout=10)

    def test_concurrent_prune_read_whole_lines_only(self):
        """Hammer appends (forcing prunes) with concurrent reads.

        Every snapshot must hold whole entries only: dicts with
        the writer's full key set. Torn lines would either fail
        to parse or surface truncated dicts.
        """
        path = os.environ["BUTTONS_LEDGER_PATH"]
        with open(path, "w") as f:
            for seq in range(20):
                f.write(json.dumps(
                    {"kind": "run", "seq": seq, "marker": f"m-{seq}",
                     "ts": 1}) + "\n")
        stop = threading.Event()
        errors = []
        saw_data = threading.Event()

        def writer():
            seq = 1000
            try:
                while not stop.is_set():
                    ledger.append({"kind": "run", "seq": seq,
                                   "marker": f"m-{seq}"})
                    seq += 1
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def reader():
            try:
                while not stop.is_set():
                    for entry in ledger.read(source="console",
                                             limit=50):
                        if not isinstance(entry, dict):
                            errors.append(
                                f"non-dict entry: {entry!r}")
                            continue
                        saw_data.set()
                        seq = entry.get("seq")
                        if entry.get("marker") != f"m-{seq}":
                            errors.append(
                                f"torn entry: {entry!r}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        with mock.patch.object(
                ledger, "LEDGER_MAX_BYTES", 500), \
                mock.patch.object(
                    ledger, "LEDGER_PRUNE_LINES", 10):
            threads = ([threading.Thread(target=writer)]
                       + [threading.Thread(target=reader)
                          for _ in range(4)])
            for thread in threads:
                thread.start()
            stop.wait(timeout=2)
            stop.set()
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertTrue(saw_data.is_set(), "no reader ever saw data")


if __name__ == "__main__":
    unittest.main()
