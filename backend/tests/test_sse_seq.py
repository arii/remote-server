"""Bounded SSE replay ids (issue #1208): additive monotonic seq.

Every SSE JSON frame rides a process-wide monotonic `seq`
(backward compatible: old clients ignore it). Token buffers record
the same seqs, so reattach replays re-emit the ORIGINAL ids and a
client that applied the live prefix drops the replayed prefix by
seq instead of doubling it. Generators alias payloads into
change-detection state, so sse_frame must copy, not mutate.
Run: PYTHONPATH=./deep-agent python3 -m unittest \
    backend.tests.test_sse_seq
"""

import json
import unittest

from backend.api.routers import chat


def _payload(frame):
    """(event, data-dict) for one sse_frame string."""
    event = None
    data = []
    for line in frame.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:") :].strip())
    return event, json.loads("".join(data))


class SseFrameSeqTest(unittest.TestCase):
    def test_frames_carry_strictly_increasing_int_seq(self):
        seqs = [
            _payload(chat.sse_frame("token", {"token": "x"}))[1]["seq"]
            for _ in range(50)
        ]
        for seq in seqs:
            self.assertIsInstance(seq, int)
            self.assertGreater(seq, 0)
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))

    def test_explicit_seq_preserved_for_replay(self):
        # Reattach re-emits stored ids via setdefault: an explicit
        # seq must survive sse_frame untouched.
        event, payload = _payload(chat.sse_frame("token", {"token": "x", "seq": 41}))
        self.assertEqual(event, "token")
        self.assertEqual(payload["seq"], 41)
        self.assertEqual(payload["token"], "x")

    def test_frame_copies_caller_payload(self):
        # Generators alias payload dicts into change-detection
        # state; injecting seq in place would poison the `last`
        # comparison and spam duplicate frames.
        data = {"token": "x"}
        chat.sse_frame("token", data)
        self.assertEqual(data, {"token": "x"})

    def test_error_frames_carry_seq_additively(self):
        # The additive contract: pre-fix payloads were exactly
        # {"error": msg}; now they are {"error": msg, "seq": N} —
        # same error identity, one added key.
        frames = list(chat._chat_events("", "remote-server", "s1"))
        self.assertTrue(frames)
        event, payload = _payload(frames[0])
        self.assertEqual(event, "error")
        self.assertEqual(payload["error"], "empty prompt")
        self.assertEqual(set(payload), {"error", "seq"})
        self.assertIsInstance(payload["seq"], int)

    def test_bad_prefer_error_keeps_identity(self):
        frames = list(chat._chat_events("hi", "remote-server", "s2", prefer="Muse"))
        event, payload = _payload(frames[0])
        self.assertEqual(event, "error")
        self.assertEqual(payload["error"], "bad prefer")
        self.assertEqual(set(payload), {"error", "seq"})


class ReplaySeqTest(unittest.TestCase):
    def setUp(self):
        self._sessions = dict(chat.chat_sessions)
        self.addCleanup(self._restore)

    def _restore(self):
        chat.chat_sessions.clear()
        chat.chat_sessions.update(self._sessions)

    def _done_session(self, session_id, tokens):
        chat.chat_sessions[session_id] = {
            "tokens": [],
            "seqs": [],
            "done": False,
            "final": None,
        }
        seqs = [chat._buffer_token(session_id, tok) for tok in tokens]
        sess = chat.chat_sessions[session_id]
        sess["done"] = True
        sess["final"] = {"text": "".join(tokens)}
        return seqs

    def test_buffer_token_returns_stable_int_seq(self):
        chat.chat_sessions["seq-probe"] = {"tokens": [], "seqs": [], "done": False}
        first = chat._buffer_token("seq-probe", "a")
        second = chat._buffer_token("seq-probe", "b")
        self.assertIsInstance(first, int)
        self.assertGreater(second, first)
        self.assertEqual(chat.chat_sessions["seq-probe"]["seqs"], [first, second])
        self.assertIsNone(chat._buffer_token("seq-probe", ""))

    def test_reattach_replays_original_seqs_in_order(self):
        # A client that applied the live prefix drops the replayed
        # prefix by seq: the replay must carry the SAME ids, in
        # order, so the client's high-water mark covers them.
        live_seqs = self._done_session("replay-s1", ["he", "llo"])
        frames = [_payload(f) for f in chat._reattach_events("replay-s1")]
        tokens = [(p["token"], p.get("seq")) for e, p in frames if e == "token"]
        self.assertEqual(tokens, [("he", live_seqs[0]), ("llo", live_seqs[1])])
        done = [p for e, p in frames if e == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
