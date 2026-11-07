"""Visual impact tool tests (#849). Hermetic: canned td ux JSON +
fixture PNGs encoded in-test (no binary fixtures, no network, no
live model except the stub-injected ask_diff path).
Run: python3 tests/test_impact.py  (A only under system python)
     deep-agent/.venv/bin/python tests/test_impact.py  (A + B)"""
import base64
import binascii
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import impact

try:
    import langchain_core  # noqa: F401
    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


def rgb(w, h, color=(10, 20, 30)):
    return [bytes(bytearray(color) * w) for _ in range(h)]


def shot(w=64, h=48, color=(10, 20, 30)):
    return impact.png_encode_rgb(w, h, rgb(w, h, color))


def result_doc(route="/", viewport="desktop-1280", **over):
    doc = {"route": route, "viewport": viewport,
           "timestamp": "2026-11-04T00:00:00Z",
           "accessibility": {"violations": []},
           "overflow": [], "images": [], "tapTargets": [],
           "aboveTheFold": {"heroViewportPercentage": 60,
                            "visibleCTACount": 1,
                            "hasPrimaryCTA": True},
           "screenshot": "screenshots/home-desktop-1280.png"}
    doc.update(over)
    return doc


def write_capture(root, key="home-desktop-1280", doc=None,
                  png=None, light=None):
    os.makedirs(os.path.join(root, "results"), exist_ok=True)
    os.makedirs(os.path.join(root, "screenshots"), exist_ok=True)
    with open(os.path.join(root, "results", key + ".json"),
              "w", encoding="utf-8") as f:
        json.dump(doc if doc is not None else result_doc(), f)
    with open(os.path.join(root, "screenshots", key + ".png"),
              "wb") as f:
        f.write(png if png is not None else shot())
    if light is not None:
        os.makedirs(os.path.join(root, "lighthouse"), exist_ok=True)
        with open(os.path.join(root, "lighthouse", "home.json"),
                  "w", encoding="utf-8") as f:
            json.dump(light, f)
    return root


class _StubModel:
    def __init__(self, reply="canned verdict"):
        self.reply = reply
        self.seen = None

    def invoke(self, messages):
        self.seen = messages
        return _Msg(self.reply)


class _Msg:
    def __init__(self, content):
        self.content = content


class TestRoutes(unittest.TestCase):
    def test_valid_route(self):
        for good in ("/", "/hrm", "/hrm/client/control?a=1",
                     "/a-b_c.d~e:f/g"):
            self.assertTrue(impact.valid_route(good), good)
        for bad in ("", "hrm", "/a b", "/a/../b", "/a\\b",
                    "/x" * 200, None, 42, "/tab\there"):
            self.assertFalse(impact.valid_route(bad), bad)


class TestRunUxCapture(unittest.TestCase):
    def _proc(self, stdout, rc=0, stderr=""):
        return subprocess.CompletedProcess(
            ["td"], rc, stdout=stdout, stderr=stderr)

    def test_ok_parses_results(self):
        payload = {"status": "success",
                   "results": [{"route": "/", "status": "success"}]}
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(
                    subprocess, "run",
                    return_value=self._proc(json.dumps(payload))) as m:
                out = impact.run_ux_capture(d, route="/hrm")
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["results"]), 1)
        argv = m.call_args[0][0]
        self.assertEqual(argv[:4], ["td", "--json", "ux", "audit"])
        self.assertIn("/hrm", argv)
        self.assertIn("results", out["artifacts"])

    def test_screenshots_subcommand(self):
        payload = {"status": "success", "results": []}
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(
                    subprocess, "run",
                    return_value=self._proc(json.dumps(payload))) as m:
                out = impact.run_ux_capture(d, screenshots_only=True)
        self.assertTrue(out["ok"])
        self.assertEqual(m.call_args[0][0][:4],
                         ["td", "--json", "ux", "screenshots"])

    def test_td_error_is_data(self):
        payload = {"status": "error", "message": "boom"}
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(
                    subprocess, "run",
                    return_value=self._proc(json.dumps(payload), rc=1)):
                out = impact.run_ux_capture(d)
        self.assertFalse(out["ok"])
        self.assertIn("boom", out["error"])

    def test_bad_json_timeout_exec(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(
                    subprocess, "run",
                    return_value=self._proc("not json", rc=1,
                                            stderr="kaput")):
                out = impact.run_ux_capture(d)
            self.assertFalse(out["ok"])
            self.assertIn("bad json", out["error"])
            with mock.patch.object(
                    subprocess, "run",
                    side_effect=subprocess.TimeoutExpired("td", 1)):
                out = impact.run_ux_capture(d)
            self.assertIn("TIMEOUT", out["error"])
            with mock.patch.object(
                    subprocess, "run",
                    side_effect=OSError("no td")):
                out = impact.run_ux_capture(d)
            self.assertIn("exec failed", out["error"])

    def test_bad_inputs_raise(self):
        with self.assertRaises(ValueError):
            impact.run_ux_capture("/no/such/dir")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                impact.run_ux_capture(d, route="nope")
            with self.assertRaises(ValueError):
                impact.run_ux_capture(d, route="/", all_routes=True)
            with self.assertRaises(ValueError):
                impact.run_ux_capture(d, desktop=True, mobile=True)


class TestCollectSnapshot(unittest.TestCase):
    def test_collect_reads_all_three(self):
        light = {"categories": {"performance": {"score": 0.9}}}
        with tempfile.TemporaryDirectory() as d:
            cap = write_capture(os.path.join(d, "cap"), light=light)
            snap = impact.collect_capture(cap)
        self.assertIn("home-desktop-1280", snap["results"])
        self.assertIn("home-desktop-1280", snap["screenshots"])
        self.assertEqual(snap["lighthouse"]["home"], light)
        self.assertTrue(snap["screenshots"]
                        ["home-desktop-1280"].endswith(".png"))

    def test_collect_skips_bad_json(self):
        with tempfile.TemporaryDirectory() as d:
            cap = os.path.join(d, "cap")
            os.makedirs(os.path.join(cap, "results"))
            with open(os.path.join(cap, "results", "bad.json"),
                      "w") as f:
                f.write("{nope")
            snap = impact.collect_capture(cap)
        self.assertEqual(snap["results"], {})

    def test_collect_bad_dir_raises(self):
        with self.assertRaises(ValueError):
            impact.collect_capture("/no/such/dir")

    def test_snapshot_freezes(self):
        with tempfile.TemporaryDirectory() as d:
            cap = os.path.join(d, "artifacts", "ux-audit")
            write_capture(cap)
            dest = impact.snapshot_capture(d, "base")
            self.assertTrue(dest.endswith(
                os.path.join("ux-impact", "base")))
            self.assertTrue(os.path.isfile(os.path.join(
                dest, "results", "home-desktop-1280.json")))
            with self.assertRaises(ValueError):
                impact.snapshot_capture(d, "base")  # exists
            with self.assertRaises(ValueError):
                impact.snapshot_capture(d, "../evil")
            with self.assertRaises(ValueError):
                impact.snapshot_capture(d, "")

    def test_snapshot_needs_capture(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                impact.snapshot_capture(d, "base")


class TestDiffJson(unittest.TestCase):
    def _diff(self, before_doc, after_doc):
        with tempfile.TemporaryDirectory() as d:
            b = write_capture(os.path.join(d, "b"), doc=before_doc)
            a = write_capture(os.path.join(d, "a"), doc=after_doc)
            return impact.diff_versions(b, a)

    def test_identical_is_clean(self):
        out = self._diff(result_doc(), result_doc())
        self.assertEqual(out["findings"], [])
        self.assertEqual(out["compared"], 1)
        self.assertEqual(out["summary"]["high"], 0)

    def test_new_overflow_is_high(self):
        after = result_doc(overflow=[{"tagName": "DIV", "id": "x",
                                      "className": "wide",
                                      "rect": {"left": 0,
                                              "right": 2000,
                                              "width": 2000}}])
        out = self._diff(result_doc(), after)
        self.assertEqual(len(out["findings"]), 1)
        hit = out["findings"][0]
        self.assertEqual(hit["severity"], "high")
        self.assertIn("overflow", hit["title"])
        self.assertEqual(hit["route"], "/")
        self.assertEqual(hit["viewport"], "desktop-1280")

    def test_new_axe_violation_ranked_by_impact(self):
        def axe(vid, imp):
            return {"violations": [
                {"id": vid, "impact": imp, "nodes": [
                    {"target": ["#main"]}]}]}
        after = result_doc(accessibility=axe("color-contrast",
                                             "serious"))
        out = self._diff(result_doc(), after)
        self.assertEqual(out["findings"][0]["severity"], "high")
        self.assertIn("color-contrast",
                      out["findings"][0]["title"])
        grown = result_doc(accessibility=axe("x", "minor"))
        grown2 = result_doc(accessibility={
            "violations": [{"id": "x", "impact": "minor", "nodes": [
                {"target": ["#a"]}, {"target": ["#b"]}]}]})
        out = self._diff(grown, grown2)
        self.assertIn("grew", out["findings"][0]["title"])

    def test_new_image_alt_and_size(self):
        after = result_doc(images=[
            {"src": "https://x/i.png", "alt": "",
             "naturalWidth": 2000, "naturalHeight": 100,
             "renderedWidth": 100, "renderedHeight": 50}])
        out = self._diff(result_doc(), after)
        titles = [h["title"] for h in out["findings"]]
        self.assertIn("new image without alt text", titles)
        self.assertIn("new oversized image", titles)

    def test_new_tap_targets_medium(self):
        after = result_doc(tapTargets=[
            {"tagName": "BUTTON", "text": "go", "width": 20,
             "height": 20}])
        out = self._diff(result_doc(), after)
        self.assertEqual(out["findings"][0]["severity"], "medium")

    def test_fold_change_is_low(self):
        before = result_doc()
        after = result_doc(aboveTheFold={
            "heroViewportPercentage": 60, "visibleCTACount": 0,
            "hasPrimaryCTA": False})
        out = self._diff(before, after)
        self.assertEqual(out["findings"][0]["severity"], "low")
        self.assertIn("above-the-fold", out["findings"][0]["title"])

    def test_added_removed_routes(self):
        with tempfile.TemporaryDirectory() as d:
            b = write_capture(os.path.join(d, "b"))
            a = os.path.join(d, "a")
            os.makedirs(os.path.join(a, "results"))
            os.makedirs(os.path.join(a, "screenshots"))
            write_capture(a, key="other-mobile-375",
                          doc=result_doc(route="/other",
                                         viewport="mobile-375"))
            out = impact.diff_versions(b, a)
        by_title = {h["title"]: h for h in out["findings"]}
        self.assertEqual(
            by_title["route missing from after capture"]
            ["severity"], "medium")
        self.assertEqual(
            by_title["route captured only in after"]
            ["severity"], "info")
        self.assertEqual(out["added"], ["other-mobile-375"])
        self.assertEqual(out["removed"], ["home-desktop-1280"])

    def test_ranking_critical_first(self):
        ranked = impact.rank_findings([
            {"severity": "info", "title": "b", "route": "/"},
            {"severity": "high", "title": "a", "route": "/"},
            {"severity": "critical", "title": "c",
             "route": "/"}])
        self.assertEqual([h["severity"] for h in ranked],
                         ["critical", "high", "info"])


def hand_png(width, height, ctype, filtered_rows):
    flat = b"".join(bytes([f]) + bytes(r)
                    for f, r in filtered_rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, ctype,
                       0, 0, 0)
    idat = zlib.compress(flat)
    out = bytearray(b"\x89PNG\r\n\x1a\n")
    for ctype_b, body in ((b"IHDR", ihdr), (b"IDAT", idat)):
        out += struct.pack(">I", len(body)) + ctype_b + body
        out += struct.pack(">I", binascii.crc32(ctype_b + body)
                           & 0xFFFFFFFF)
    out += struct.pack(">I", 0) + b"IEND"
    out += struct.pack(">I", binascii.crc32(b"IEND") & 0xFFFFFFFF)
    return bytes(out)


class TestPngCodec(unittest.TestCase):
    def test_roundtrip_rgb(self):
        rows = rgb(16, 12)
        w, h, back = impact.png_decode_rgb(
            impact.png_encode_rgb(16, 12, rows))
        self.assertEqual((w, h), (16, 12))
        self.assertEqual(back, rows)

    def test_filters_reconstruct(self):
        row0 = [10, 20, 30, 40, 50, 60]
        # Up: stored row1 + row0 = raw row1.
        p = hand_png(2, 2, 2, [(0, row0), (2, [1] * 6)])
        back = impact.png_decode_rgb(p)[2]
        self.assertEqual(back[1], bytes([11, 21, 31, 41, 51, 61]))
        # Sub: stored px1 + px0 = raw px1.
        p = hand_png(2, 1, 2, [(1, [7, 8, 9, 1, 1, 1])])
        self.assertEqual(impact.png_decode_rgb(p)[2],
                         [bytes([7, 8, 9, 8, 9, 10])])
        # Average with zero upper row == stored + left/2.
        p = hand_png(2, 1, 2, [(3, [10, 10, 10, 10, 10, 10])])
        got = impact.png_decode_rgb(p)[2][0]
        self.assertEqual(list(got),
                         [10, 10, 10, 15, 15, 15])
        # Unknown filter byte degrades to None, never raises.
        p = hand_png(2, 1, 2, [(9, [0] * 6)])
        self.assertIsNone(impact.png_decode_rgb(p))

    def test_rgba_alpha_stripped(self):
        p = hand_png(1, 1, 6, [(0, [255, 0, 0, 255])])
        w, h, rows = impact.png_decode_rgb(p)
        self.assertEqual(rows, [bytes([255, 0, 0])])

    def test_unsupported_and_corrupt(self):
        self.assertIsNone(impact.png_decode_rgb(b"nope"))
        self.assertIsNone(impact.png_size(b"nope"))
        # Palette (ctype 3) is out of the decoded subset.
        p = hand_png(1, 1, 3, [(0, [0])])
        self.assertIsNone(impact.png_decode_rgb(p))
        good = shot()
        # Truncated mid-IDAT (not just a missing IEND, which the
        # decoder tolerates) degrades to None.
        self.assertIsNone(impact.png_decode_rgb(good[:60]))
        # Absurd dimensions raise instead of allocating.
        ihdr = struct.pack(">IIBBBBB", 30000, 30000, 8, 2, 0, 0, 0)
        evil = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13)
                + b"IHDR" + ihdr
                + struct.pack(">I", binascii.crc32(b"IHDR" + ihdr)
                              & 0xFFFFFFFF))
        with self.assertRaises(ValueError):
            impact.png_decode_rgb(evil)
        with self.assertRaises(ValueError):
            impact.png_encode_rgb(4, 2, rgb(4, 1))

    def test_diff_regions(self):
        same = shot()
        out = impact.diff_regions(same, same)
        self.assertFalse(out["changed"])
        self.assertTrue(out["exact"])
        rows = rgb(64, 48)
        rows[0] = bytes([200, 0, 0]) * 64
        changed = impact.png_encode_rgb(64, 48, rows)
        out = impact.diff_regions(same, changed, cell=16)
        self.assertTrue(out["changed"])
        self.assertTrue(out["exact"])
        self.assertEqual(len(out["cells"]), 4)
        self.assertAlmostEqual(out["changed_ratio"], 4 / 12)
        # Dimension change: one full-frame cell.
        tall = shot(64, 64)
        out = impact.diff_regions(same, tall)
        self.assertEqual(out["cells"], [(0, 0, 64, 64)])
        # Undecodable pair: bytes-compare, no regions.
        out = impact.diff_regions(b"a", b"b")
        self.assertTrue(out["changed"])
        self.assertFalse(out["exact"])
        self.assertEqual(out["cells"], [])

    def test_downscale_and_crop(self):
        big = shot(1600, 1200)
        small = impact.downscale_png(big, 800)
        self.assertEqual(impact.png_size(small), (800, 600))
        tiny = shot(8, 8)
        self.assertEqual(impact.downscale_png(tiny, 800), tiny)
        crop = impact.crop_png(shot(64, 48), (0, 0, 16, 16))
        self.assertEqual(impact.png_size(crop), (16, 16))
        # Clamped + bad boxes return the original bytes.
        full = shot(64, 48)
        self.assertEqual(impact.png_size(
            impact.crop_png(full, (1000, 1000, 4, 4))), (64, 48))
        self.assertEqual(impact.crop_png(full, "bogus"), full)
        self.assertEqual(impact.downscale_png(b"junk"), b"junk")

    def test_shot_pair_in_diff(self):
        rows = rgb(64, 48)
        rows[5] = bytes([9, 9, 9]) * 64
        with tempfile.TemporaryDirectory() as d:
            b = write_capture(os.path.join(d, "b"))
            a = write_capture(
                os.path.join(d, "a"),
                png=impact.png_encode_rgb(64, 48, rows))
            out = impact.diff_versions(b, a)
        hits = [h for h in out["findings"]
                if h["title"] == "visual change in screenshot"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "low")
        self.assertIn("region(s)", hits[0]["evidence"])


class TestLighthouse(unittest.TestCase):
    def _light(self, perf, a11y=0.9):
        return {"categories": {"performance": {"score": perf},
                               "accessibility": {"score": a11y}}}

    def test_read_summarizes(self):
        with tempfile.TemporaryDirectory() as d:
            cap = write_capture(os.path.join(d, "cap"),
                                light=self._light(0.83))
            out = impact.lighthouse_read(cap)
        self.assertIn("performance=0.83", out["text"])
        self.assertEqual(out["scores"]["home"]["performance"], 0.83)

    def test_read_empty(self):
        with tempfile.TemporaryDirectory() as d:
            cap = write_capture(os.path.join(d, "cap"))
            out = impact.lighthouse_read(cap)
        self.assertEqual(out["text"], "no lighthouse artifacts")

    def test_diff_flags_drops(self):
        before = {"home": self._light(0.95)}
        after = {"home": self._light(0.60)}  # -0.35 -> high
        out = impact.lighthouse_diff(before, after)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "high")
        self.assertIn("performance", out[0]["title"])
        out = impact.lighthouse_diff(
            before, {"home": self._light(0.80)})  # -0.15
        self.assertEqual(out[0]["severity"], "medium")
        self.assertEqual(impact.lighthouse_diff(
            before, {"home": self._light(0.90)}), [])  # -0.05
        self.assertEqual(impact.lighthouse_diff(
            before, {"home": self._light(0.99)}), [])  # gain

    def test_diff_rides_diff_versions(self):
        with tempfile.TemporaryDirectory() as d:
            b = write_capture(os.path.join(d, "b"),
                              light=self._light(0.95))
            a = write_capture(os.path.join(d, "a"),
                              light=self._light(0.50))
            out = impact.diff_versions(b, a)
        hits = [h for h in out["findings"]
                if h["title"].startswith("lighthouse")]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "high")


class TestEvidenceAndAsk(unittest.TestCase):
    def test_format_evidence_capped(self):
        diff = {"compared": 3, "findings": [
            {"severity": "high", "title": "t%d" % i,
             "route": "/", "viewport": "v",
             "evidence": "e" * 20} for i in range(60)],
            "summary": {"high": 60}}
        text = impact.format_evidence_for_model("what changed?",
                                                diff)
        self.assertIn("compared 3", text)
        self.assertIn("+20 more findings", text)
        self.assertLessEqual(len(text), 6000 + 30)
        short = impact.format_evidence_for_model(
            "q", {"compared": 0, "findings": [], "summary": {}},
            limit=50)
        self.assertIn("[evidence truncated]", short)

    def test_build_diff_content(self):
        parts = impact.build_diff_content("q?", "evidence text",
                                          images=[shot(8, 8)])
        self.assertEqual(parts[0]["type"], "text")
        self.assertIn("q?", parts[0]["text"])
        self.assertEqual(parts[1]["type"], "image_url")
        url = parts[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        raw = base64.b64decode(url.split(",", 1)[1])
        self.assertEqual(impact.png_size(raw), (8, 8))
        with self.assertRaises(ValueError):
            impact.build_diff_content("", "e")
        with self.assertRaises(ValueError):
            impact.build_diff_content("q", "  ")

    def test_ask_diff_stub_model(self):
        stub = _StubModel("verdict: ship it")
        out = impact.ask_diff("what changed?", "evidence",
                              images=[shot(8, 8)], _model=stub)
        self.assertEqual(out, "verdict: ship it")
        self.assertEqual(len(stub.seen), 2)
        self.assertEqual(stub.seen[1]["type"], "image_url")

    def test_ask_diff_stub_failure_is_data(self):
        class _Boom:
            def invoke(self, messages):
                raise RuntimeError("down")
        out = impact.ask_diff("q", "e", _model=_Boom())
        self.assertTrue(out.startswith("ASK_ERROR:"), out)

    def test_ask_diff_no_backend_is_data(self):
        # _model=None with no deep_chat importable: hermetic only
        # when deep_chat is uninstallable; otherwise this leg runs
        # Part B. Here assert the validation still fires first.
        with self.assertRaises(ValueError):
            impact.ask_diff("", "e", _model=_StubModel())

    def test_changed_crops(self):
        rows = rgb(64, 48)
        rows[5] = bytes([9, 9, 9]) * 64
        with tempfile.TemporaryDirectory() as d:
            b = write_capture(os.path.join(d, "b"))
            a = write_capture(
                os.path.join(d, "a"),
                png=impact.png_encode_rgb(64, 48, rows))
            crops = impact.changed_crops(
                impact.collect_capture(b),
                impact.collect_capture(a))
        self.assertEqual(len(crops), 1)
        w, h = impact.png_size(crops[0])
        self.assertLessEqual(max(w, h), impact.ASK_MAX_DIM)
        # Identical pair yields no crops.
        with tempfile.TemporaryDirectory() as d:
            b = write_capture(os.path.join(d, "b"))
            crops = impact.changed_crops(
                impact.collect_capture(b),
                impact.collect_capture(b))
        self.assertEqual(crops, [])


class TestResolveAndFormat(unittest.TestCase):
    def test_resolve_guards(self):
        with tempfile.TemporaryDirectory() as d:
            inner = os.path.join(d, "cap")
            os.makedirs(inner)
            self.assertEqual(
                impact.resolve_capture_dir(d, "cap"), inner)
            self.assertEqual(
                impact.resolve_capture_dir(d, inner), inner)
            self.assertIsNone(
                impact.resolve_capture_dir(d, "/etc"))
            self.assertIsNone(
                impact.resolve_capture_dir(d, "../escape"))
            self.assertIsNone(
                impact.resolve_capture_dir(d, ""))
            self.assertIsNone(
                impact.resolve_capture_dir(d, "missing"))

    def test_format_diff_text(self):
        out = impact.format_diff_text(
            {"compared": 1, "findings": [], "summary": {}})
        self.assertIn("no findings", out)
        out = impact.format_diff_text(
            {"compared": 1,
             "findings": [{"severity": "high", "title": "t",
                           "route": "/r", "viewport": "v",
                           "evidence": "e"}],
             "summary": {"high": 1}})
        self.assertIn("[high] t (/r [v]): e", out)


class TestCli(unittest.TestCase):
    def test_usage(self):
        self.assertEqual(impact.main([]), 2)
        self.assertEqual(impact.main(["bogus"]), 2)

    def test_diff_cli(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            b = write_capture(os.path.join(d, "b"))
            a = write_capture(os.path.join(d, "a"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = impact.main(["diff", b, a])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["compared"], 1)

    def test_lighthouse_cli(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            cap = write_capture(os.path.join(d, "cap"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = impact.main(["lighthouse", cap])
        self.assertEqual(rc, 0)
        self.assertIn("no lighthouse", buf.getvalue())

    def test_snapshot_cli_needs_capture(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(impact.main(["snapshot", d, "x"]), 2)


class TestZeroGemini(unittest.TestCase):
    def test_no_gemini_reference(self):
        # DoD: the impact path never touches Gemini. The module
        # docstring + comments may say "zero-Gemini"/"never
        # Gemini" (this assertion allows those spellings); any
        # other mention (key, client, model id) fails.
        src = os.path.join(os.path.dirname(__file__), "..",
                           "deep-agent", "impact.py")
        with open(src, encoding="utf-8") as f:
            text = f.read()
        scrubbed = text.replace("Zero-Gemini", "").replace(
            "zero Gemini", "").replace("never Gemini", "")
        self.assertNotIn("emini", scrubbed.lower())

    def test_model_path_is_router_backed(self):
        # ask_diff resolves its model only through
        # deep_chat.build_model (Ollama first, Nebius fallback).
        src = os.path.join(os.path.dirname(__file__), "..",
                           "deep-agent", "impact.py")
        with open(src, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("deep_chat.build_model", text)
        self.assertNotIn("ChatOllama", text)
        self.assertNotIn("ChatOpenAI", text)
        self.assertNotIn("API_KEY", text)


# Ask-set registration died with the ask CLI (#1131): the 12-tool
# create_agent assembly lived only in main(). Impact-tool wiring is
# still pinned via deep_chat.build_tools(impact_tools=True) below.


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestChatRegistration(unittest.TestCase):
    def test_default_nine_preserved(self):
        import deep_chat
        tools = deep_chat.build_tools("/tmp")
        self.assertEqual(len(tools), 9)

    def test_opt_in_adds_four(self):
        import deep_chat
        tools = deep_chat.build_tools("/tmp", impact_tools=True)
        self.assertEqual(len(tools), 13)
        by_name = {t.name: t for t in tools}
        for name in ("run_ux_capture", "diff_versions", "ask_diff",
                     "lighthouse_read"):
            self.assertIn(name, by_name)

    def test_diff_tool_invokes(self):
        import deep_chat
        with tempfile.TemporaryDirectory() as d:
            b = write_capture(os.path.join(d, "b"))
            a = write_capture(
                os.path.join(d, "a"),
                doc=result_doc(overflow=[{"tagName": "DIV"}]))
            by_name = {t.name: t for t in deep_chat.build_tools(
                d, impact_tools=True)}
            out = by_name["diff_versions"].invoke(
                {"before": "b", "after": "a"})
        self.assertIn("overflow", out)
        self.assertIn("high=1", out)

    def test_tools_reject_escapes(self):
        import deep_chat
        with tempfile.TemporaryDirectory() as d:
            by_name = {t.name: t for t in deep_chat.build_tools(
                d, impact_tools=True)}
            out = by_name["diff_versions"].invoke(
                {"before": "/etc", "after": "b"})
            self.assertIn("REJECTED", out)
            out = by_name["run_ux_capture"].invoke({"route": "x"})
            self.assertIn("REJECTED", out)
            out = by_name["lighthouse_read"].invoke(
                {"capture": "/etc"})
            self.assertIn("REJECTED", out)

    def test_capture_tool_reports_td_failure(self):
        import deep_chat
        with tempfile.TemporaryDirectory() as d:
            by_name = {t.name: t for t in deep_chat.build_tools(
                d, impact_tools=True)}
            with mock.patch.object(
                    impact, "run_ux_capture",
                    return_value={"ok": False,
                                  "error": "no browser"}):
                out = by_name["run_ux_capture"].invoke(
                    {"route": "/hrm"})
        self.assertIn("CAPTURE_ERROR", out)

    def test_ask_tool_uses_model_path(self):
        import deep_chat
        with tempfile.TemporaryDirectory() as d:
            b = write_capture(os.path.join(d, "b"))
            a = write_capture(os.path.join(d, "a"))
            by_name = {t.name: t for t in deep_chat.build_tools(
                d, impact_tools=True)}
            with mock.patch.object(
                    impact, "ask_diff",
                    return_value="verdict: clean") as m:
                out = by_name["ask_diff"].invoke(
                    {"question": "what changed?",
                     "before": "b", "after": "a"})
        self.assertEqual(out, "verdict: clean")
        _q, evidence = m.call_args[0][:2]
        self.assertIn("compared 1", evidence)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestAskToolsInvoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ask_investigator",
            os.path.join(os.path.dirname(__file__), "..",
                         "deep-agent", "ask_investigator.py"))
        cls.ai = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ai)

    def test_impact_tools_invoke(self):
        ai = self.__class__.ai
        with tempfile.TemporaryDirectory() as d:
            prev = ai._checkout
            ai._checkout = d
            try:
                b = write_capture(os.path.join(d, "b"))
                a = write_capture(os.path.join(d, "a"))
                out = ai.diff_versions.invoke(
                    {"before": "b", "after": "a"})
                self.assertIn("no findings", out)
                out = ai.lighthouse_read.invoke({"capture": "b"})
                self.assertIn("no lighthouse", out)
                out = ai.run_ux_capture.invoke({"route": "x"})
                self.assertIn("REJECTED", out)
            finally:
                ai._checkout = prev


if __name__ == "__main__":
    unittest.main()
