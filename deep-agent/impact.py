"""Visual impact analysis: td ux capture + local-model diff tools.

Zero-Gemini path for pre-merge UI verification: capture via ``td ux``
(Playwright stays there; this module never screenshots anything
itself), diff DOM/audit JSON + screenshot pairs across two captures,
and interrogate the diff with a local model (Ollama first, Nebius
fallback) through deep_chat.build_model.

Stdlib-only at import (same contract as s5tools/model_router): the
capture/diff/PNG/lighthouse helpers and finding shape import under
system python; only ask_diff's real-model path needs the venv
(langchain via deep_chat), and tests inject a stub model instead.

Capture layout (written by td ux in the target checkout):
  artifacts/ux-audit/results/<slug>-<viewport>.json
  artifacts/ux-audit/screenshots/<slug>-<viewport>.png
  artifacts/ux-audit/lighthouse/<slug>.json

Finding shape (shared with the #845 review synthesis; coordinate,
don't duplicate):
  {"severity": ..., "title": ..., "route": ..., "viewport": ...,
   "evidence": ...}
severity in critical/high/medium/low/info.

CLI:
  impact.py capture <checkout> [--route R|--all-routes] [--desktop|--mobile]
                    [--screenshots-only]
  impact.py snapshot <checkout> <label>     # freeze current artifacts
  impact.py diff <before-dir> <after-dir>
  impact.py ask "<question>" <diff-json>     # needs the venv + a model
  impact.py lighthouse <capture-dir>
"""

import base64
import binascii
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

import cli

cli.bootstrap()

UX_DIR = os.path.join("artifacts", "ux-audit")
RESULTS_DIR = "results"
SHOTS_DIR = "screenshots"
LIGHTHOUSE_DIR = "lighthouse"

CAPTURE_TIMEOUT = 600
SEVERITIES = ("critical", "high", "medium", "low", "info")
_SEV_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

# Route arg for td ux: absolute path, no whitespace/controls, no
# traversal. td passes it to the audit runner, which joins it onto
# baseUrl — validation here keeps one bad arg from escaping that.
ROUTE_RE = re.compile(r"^/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*$")
MAX_ROUTE = 200

# PNG decode guard: refuse absurd dimensions before allocating.
PNG_MAX_PIXELS = 64_000_000
# Model-turn image budget: downscale target + crop size so a turn
# never carries full-res dumps.
ASK_MAX_DIM = 800

# Axe impact -> finding severity.
AXE_SEVERITY = {"critical": "high", "serious": "high",
                "moderate": "medium", "minor": "low"}
# Lighthouse score drops worth flagging (0..1 scale).
LIGHTHOUSE_DROP_MEDIUM = 0.10
LIGHTHOUSE_DROP_HIGH = 0.25


def valid_route(route):
    """True for a td ux --route value safe to pass through."""
    return (isinstance(route, str)
            and 1 <= len(route) <= MAX_ROUTE
            and bool(ROUTE_RE.match(route))
            and ".." not in route
            and "\\" not in route)


def capture_paths(checkout):
    """Artifact dirs for a checkout's td ux output."""
    base = os.path.join(checkout, UX_DIR)
    return {"base": base,
            "results": os.path.join(base, RESULTS_DIR),
            "screenshots": os.path.join(base, SHOTS_DIR),
            "lighthouse": os.path.join(base, LIGHTHOUSE_DIR)}


def run_ux_capture(checkout, route="", all_routes=False,
                   desktop=False, mobile=False,
                   screenshots_only=False,
                   timeout=CAPTURE_TIMEOUT):
    """Run td ux capture in a checkout. Never raises on td failure.

    Returns {"ok": True, "status": ..., "results": [...],
    "artifacts": capture_paths(checkout)} on success, or
    {"ok": False, "error": ...} when td is missing, times out, or
    reports an error. Raises ValueError only on programmer errors
    (bad checkout dir, bad route): those are caller bugs, not runs.
    The target checkout must carry scripts/ux-*.ts (td shells to
    them); baseUrl comes from its scripts/config/ux-audit.config.json.
    """
    if not isinstance(checkout, str) or not os.path.isdir(checkout):
        raise ValueError(f"bad checkout: {checkout!r}")
    if route and not valid_route(route):
        raise ValueError(f"bad route: {route!r}")
    if all_routes and route:
        raise ValueError("route and all_routes are exclusive")
    if desktop and mobile:
        raise ValueError("desktop and mobile are exclusive")
    argv = ["td", "--json", "ux",
           "screenshots" if screenshots_only else "audit"]
    if route:
        argv += ["--route", route]
    if all_routes and not screenshots_only:
        argv.append("--all-routes")
    if desktop and not screenshots_only:
        argv.append("--desktop")
    if mobile and not screenshots_only:
        argv.append("--mobile")
    try:
        proc = subprocess.run(
            argv, cwd=checkout, capture_output=True, text=True,
            timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "error": f"TIMEOUT after {timeout}s: {' '.join(argv)}"}
    except OSError as exc:
        return {"ok": False, "error": f"exec failed: {exc}"}
    try:
        payload = json.loads(proc.stdout or "")
    except (json.JSONDecodeError, ValueError):
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-300:]
        return {"ok": False,
                "error": f"td ux bad json (rc={proc.returncode}): {tail}"}
    if not isinstance(payload, dict) or payload.get("status") != "success":
        msg = ""
        if isinstance(payload, dict):
            msg = str(payload.get("message", ""))[:300]
        if not msg:
            msg = ((proc.stderr or "").strip()[:300]
                   or f"rc={proc.returncode}")
        return {"ok": False, "error": f"td ux error: {msg}"}
    return {"ok": True, "status": "success",
            "results": payload.get("results", []),
            "artifacts": capture_paths(checkout)}


def collect_capture(capture_dir):
    """Read one capture's artifacts into a diffable snapshot.

    capture_dir is a ux-audit dir (results/ + screenshots/ [+].
    lighthouse/ beneath it). Returns {"results": {key: json},
    "screenshots": {key: path}, "lighthouse": {name: json}} keyed by
    "<slug>-<viewport>" file stems; unreadable JSON files are
    skipped, never fatal. Raises ValueError on a bad dir.
    """
    if not isinstance(capture_dir, str) or not os.path.isdir(capture_dir):
        raise ValueError(f"bad capture dir: {capture_dir!r}")
    snap = {"results": {}, "screenshots": {}, "lighthouse": {}}
    results = os.path.join(capture_dir, RESULTS_DIR)
    if os.path.isdir(results):
        for name in sorted(os.listdir(results)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(results, name),
                          encoding="utf-8") as f:
                    snap["results"][name[:-5]] = json.load(f)
            except (OSError, ValueError):
                continue
    shots = os.path.join(capture_dir, SHOTS_DIR)
    if os.path.isdir(shots):
        for name in sorted(os.listdir(shots)):
            if name.endswith(".png"):
                snap["screenshots"][name[:-4]] = os.path.join(shots, name)
    light = os.path.join(capture_dir, LIGHTHOUSE_DIR)
    if os.path.isdir(light):
        for name in sorted(os.listdir(light)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(light, name),
                          encoding="utf-8") as f:
                    snap["lighthouse"][name[:-5]] = json.load(f)
            except (OSError, ValueError):
                continue
    return snap


def snapshot_capture(checkout, label):
    """Freeze the checkout's current ux-audit artifacts under label.

    Copies artifacts/ux-audit to artifacts/ux-impact/<label> so a
    base capture survives the head capture that follows it (the
    runbook's capture-base/capture-head flow). Returns the snapshot
    dir. Raises ValueError on a bad checkout/label or when no
    capture exists yet.
    """
    if not isinstance(checkout, str) or not os.path.isdir(checkout):
        raise ValueError(f"bad checkout: {checkout!r}")
    if (not isinstance(label, str) or not label
            or len(label) > 64 or "/" in label or label in (".", "..")
            or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", label)):
        raise ValueError(f"bad label: {label!r}")
    src = os.path.join(checkout, UX_DIR)
    if not os.path.isdir(src):
        raise ValueError(f"no capture yet at {src}")
    dest = os.path.join(checkout, "artifacts", "ux-impact", label)
    if os.path.exists(dest):
        raise ValueError(f"snapshot exists: {dest}")
    shutil.copytree(src, dest)
    return dest


def _finding(severity, title, route, viewport, evidence):
    return {"severity": severity, "title": title, "route": route,
            "viewport": viewport, "evidence": evidence}


def rank_findings(findings):
    """Severity-ranked findings (critical first), stable by route."""
    return sorted(findings,
                  key=lambda f: (_SEV_RANK.get(f.get("severity"), 99),
                                 f.get("route", ""), f.get("title", "")))


def _route_of(key, doc):
    if isinstance(doc, dict) and isinstance(doc.get("route"), str):
        return doc["route"]
    return key


def _viewport_of(key, doc):
    if isinstance(doc, dict) and isinstance(doc.get("viewport"), str):
        return doc["viewport"]
    return ""


def _as_list(value):
    return value if isinstance(value, list) else []


def _overflow_key(item):
    if not isinstance(item, dict):
        return repr(item)[:120]
    rect = item.get("rect") if isinstance(item.get("rect"), dict) else {}
    return "|".join(str(item.get(k, ""))[:60] for k in
                    ("tagName", "id", "className")) + "|" + "|".join(
                        str(rect.get(k, ""))[:20] for k in
                        ("left", "right", "width"))


def _axe_violations(doc):
    """{rule-id: {"impact", "count", "example"}} from axe JSON."""
    found = {}
    a11y = doc.get("accessibility") if isinstance(doc, dict) else None
    if not isinstance(a11y, dict):
        return found
    for viol in _as_list(a11y.get("violations")):
        if not isinstance(viol, dict):
            continue
        vid = viol.get("id")
        if not isinstance(vid, str) or not vid:
            continue
        nodes = _as_list(viol.get("nodes"))
        example = ""
        if nodes and isinstance(nodes[0], dict):
            target = nodes[0].get("target")
            if isinstance(target, list):
                example = ", ".join(str(t)[:60] for t in target[:2])
            elif isinstance(target, str):
                example = target[:120]
        found[vid] = {"impact": viol.get("impact", "minor"),
                      "count": len(nodes),
                      "example": example}
    return found


def _img_key(img):
    if not isinstance(img, dict):
        return repr(img)[:120]
    return str(img.get("src", ""))[:200]


def _tap_key(tap):
    if not isinstance(tap, dict):
        return repr(tap)[:120]
    return "|".join(str(tap.get(k, ""))[:40] for k in
                    ("tagName", "text", "width", "height"))


def _diff_result_json(key, before_doc, after_doc):
    """Findings from one route+viewport JSON pair (DOM-side evidence)."""
    before_doc = before_doc if isinstance(before_doc, dict) else {}
    after_doc = after_doc if isinstance(after_doc, dict) else {}
    route = _route_of(key, after_doc) or _route_of(key, before_doc)
    viewport = _viewport_of(key, after_doc) or _viewport_of(key, before_doc)
    out = []
    before_over = {_overflow_key(i) for i in _as_list(
        before_doc.get("overflow"))}
    new_over = [i for i in _as_list(after_doc.get("overflow"))
                if _overflow_key(i) not in before_over]
    if new_over:
        first = new_over[0] if isinstance(new_over[0], dict) else {}
        out.append(_finding(
            "high", f"{len(new_over)} new layout-overflow issue(s)",
            route, viewport,
            f"e.g. <{first.get('tagName', '?')}> id="
            f"{first.get('id', '')!r} class="
            f"{str(first.get('className', ''))[:80]!r}"))
    before_axe = _axe_violations(before_doc)
    for vid, cur in sorted(_axe_violations(after_doc).items()):
        prev = before_axe.get(vid)
        if prev is None:
            out.append(_finding(
                AXE_SEVERITY.get(cur["impact"], "low"),
                f"new a11y violation: {vid}", route, viewport,
                f"impact={cur['impact']} nodes={cur['count']}"
                + (f" e.g. {cur['example']}" if cur["example"]
                   else "")))
        elif cur["count"] > prev["count"]:
            out.append(_finding(
                AXE_SEVERITY.get(cur["impact"], "low"),
                f"a11y violation grew: {vid}", route, viewport,
                f"{prev['count']} -> {cur['count']} nodes"))
    before_imgs = {_img_key(i) for i in _as_list(before_doc.get("images"))}
    for img in _as_list(after_doc.get("images")):
        if not isinstance(img, dict) or _img_key(img) in before_imgs:
            continue
        if not img.get("alt"):
            out.append(_finding(
                "medium", "new image without alt text", route,
                viewport, f"src={str(img.get('src', ''))[:160]}"))
        try:
            nat = float(img.get("naturalWidth") or 0)
            rend = float(img.get("renderedWidth") or 0)
            if nat > 0 and rend > 0 and nat >= 2 * rend and rend >= 1:
                out.append(_finding(
                    "low", "new oversized image", route, viewport,
                    f"src={str(img.get('src', ''))[:160]} "
                    f"natural={nat:g} rendered={rend:g}"))
        except (TypeError, ValueError):
            pass
    before_taps = {_tap_key(t) for t in _as_list(
        before_doc.get("tapTargets"))}
    new_taps = [t for t in _as_list(after_doc.get("tapTargets"))
                if _tap_key(t) not in before_taps]
    if new_taps:
        out.append(_finding(
            "medium", f"{len(new_taps)} new small tap target(s)",
            route, viewport,
            ", ".join(str(t.get("text", t.get("tagName", "?")))[:30]
                      for t in new_taps[:3] if isinstance(t, dict))
            or f"{len(new_taps)} targets under 44px"))
    before_fold = before_doc.get("aboveTheFold")
    after_fold = after_doc.get("aboveTheFold")
    if (isinstance(before_fold, dict) and isinstance(after_fold, dict)
            and before_fold != after_fold):
        for fld in ("hasPrimaryCTA", "visibleCTACount",
                    "heroViewportPercentage"):
            if before_fold.get(fld) != after_fold.get(fld):
                out.append(_finding(
                    "low", f"above-the-fold changed: {fld}", route,
                    viewport,
                    f"{before_fold.get(fld)!r} -> "
                    f"{after_fold.get(fld)!r}"))
                break
    return out


# --- Minimal PNG codec (stdlib only: struct + zlib) ---
#
# Playwright screenshots are 8-bit non-interlaced truecolor(+alpha)
# PNGs. That subset is all we decode: anything else (palette,
# 16-bit, interlaced) degrades to bytes-compare with no regions
# instead of failing the diff.

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png_chunks(data):
    """(type, body) chunks; raises ValueError on a corrupt file."""
    if not isinstance(data, (bytes, bytearray)) \
            or bytes(data[:8]) != _PNG_SIG:
        raise ValueError("not a PNG")
    out = []
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = bytes(data[pos + 4:pos + 8])
        body = bytes(data[pos + 8:pos + 8 + length])
        if len(body) != length or pos + 12 + length > len(data):
            raise ValueError("truncated PNG chunk")
        out.append((ctype, body))
        pos += 12 + length
        if ctype == b"IEND":
            break
    return out


def png_size(data):
    """(width, height) from IHDR, or None when not a PNG."""
    try:
        for ctype, body in _png_chunks(data):
            if ctype == b"IHDR":
                w, h = struct.unpack(">II", body[:8])
                return (w, h) if w > 0 and h > 0 else None
    except ValueError:
        return None
    return None


def png_decode_rgb(data):
    """Decode 8-bit non-interlaced RGB/RGBA PNG -> (w, h, rows).

    rows is a list of bytes, each w*3 RGB. Returns None for any
    other PNG flavor or corrupt input (caller degrades to
    bytes-compare); raises ValueError on absurd dimensions.
    """
    try:
        chunks = _png_chunks(data)
    except ValueError:
        return None
    header = next((b for t, b in chunks if t == b"IHDR"), None)
    if header is None or len(header) < 13:
        return None
    w, h, depth, ctype, comp, filt, inter = struct.unpack(
        ">IIBBBBB", header[:13])
    if depth != 8 or ctype not in (2, 6) or comp != 0 \
            or filt != 0 or inter != 0:
        return None
    if w <= 0 or h <= 0 or w * h > PNG_MAX_PIXELS:
        raise ValueError(f"absurd PNG dimensions: {w}x{h}")
    raw = b"".join(b for t, b in chunks if t == b"IDAT")
    try:
        flat = zlib.decompress(raw)
    except zlib.error:
        return None
    ch = 3 if ctype == 2 else 4
    stride = w * ch
    if len(flat) != h * (stride + 1):
        return None
    rows = []
    prev = bytearray(stride)
    pos = 0
    for _ in range(h):
        ftype = flat[pos]
        pos += 1
        scan = bytearray(flat[pos:pos + stride])
        pos += stride
        if ftype == 1:  # Sub
            for i in range(ch, stride):
                scan[i] = (scan[i] + scan[i - ch]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                scan[i] = (scan[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                left = scan[i - ch] if i >= ch else 0
                scan[i] = (scan[i] + (left + prev[i]) // 2) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                left = scan[i - ch] if i >= ch else 0
                up = prev[i]
                up_left = prev[i - ch] if i >= ch else 0
                pred = _paeth(left, up, up_left)
                scan[i] = (scan[i] + pred) & 0xFF
        elif ftype != 0:
            return None
        prev = scan  # next row's Up/Average/Paeth needs the full
        # stride (alpha included); the RGB output strips below.
        if ch == 4:
            scan = bytearray(b for i, b in enumerate(scan)
                             if i % 4 != 3)
        rows.append(bytes(scan))
    return (w, h, rows)


def _paeth(left, up, up_left):
    cand = up + left - up_left
    pa, pb, pc = abs(cand - left), abs(cand - up), \
        abs(cand - up_left)
    if pa <= pb and pa <= pc:
        return left
    return up if pb <= pc else up_left


def png_encode_rgb(width, height, rows):
    """Encode w*h RGB rows (filter-0) into minimal PNG bytes."""
    if width <= 0 or height <= 0 or len(rows) != height:
        raise ValueError("bad rows for PNG encode")
    flat = b"".join(b"\x00" + bytes(r[:width * 3]).ljust(
        width * 3, b"\x00") for r in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(flat, 6)
    out = bytearray(_PNG_SIG)
    for ctype, body in ((b"IHDR", ihdr), (b"IDAT", idat)):
        out += struct.pack(">I", len(body)) + ctype + body
        out += struct.pack(">I", binascii.crc32(ctype + body)
                           & 0xFFFFFFFF)
    out += struct.pack(">I", 0) + b"IEND"
    out += struct.pack(">I", binascii.crc32(b"IEND") & 0xFFFFFFFF)
    return bytes(out)


def diff_regions(before_png, after_png, cell=32):
    """Changed regions between two screenshot bytes.

    Returns {"changed": bool, "cells": [(x, y, w, h)...],
    "changed_ratio": 0..1, "exact": bool}. exact=False means a
    side was undecodable, so changed is bytes-compare only with
    no regions. Dimension changes report one full-frame cell.
    """
    if before_png == after_png:
        size = png_size(before_png) or (0, 0)
        return {"changed": False, "cells": [], "changed_ratio": 0.0,
                "exact": True, "size": size}
    try:
        before = png_decode_rgb(before_png)
        after = png_decode_rgb(after_png)
    except ValueError:
        before = after = None
    if before is None or after is None:
        return {"changed": True, "cells": [], "changed_ratio": 1.0,
                "exact": False, "size": png_size(after_png)}
    (bw, bh, brows), (aw, ah, arows) = before, after
    if (bw, bh) != (aw, ah):
        return {"changed": True, "cells": [(0, 0, aw, ah)],
                "changed_ratio": 1.0, "exact": True,
                "size": (aw, ah)}
    cells = []
    total = 0
    for top in range(0, ah, cell):
        for left in range(0, aw, cell):
            total += 1
            w = min(cell, aw - left)
            changed = False
            for y in range(top, min(top + cell, ah)):
                if brows[y][left * 3:(left + w) * 3] != \
                        arows[y][left * 3:(left + w) * 3]:
                    changed = True
                    break
            if changed:
                cells.append((left, top, w,
                              min(cell, ah - top)))
    return {"changed": True, "cells": cells,
            "changed_ratio": (len(cells) / total) if total else 1.0,
            "exact": True, "size": (aw, ah)}


def downscale_png(data, max_dim=ASK_MAX_DIM):
    """Nearest-neighbor downscale so max(w, h) <= max_dim.

    Returns the original bytes when already small or undecodable
    (never raises on image content).
    """
    try:
        decoded = png_decode_rgb(data)
    except ValueError:
        return data
    if decoded is None:
        return data
    w, h, rows = decoded
    if max(w, h) <= max_dim or max_dim < 1:
        return data
    scale = max_dim / max(w, h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    out = []
    for y in range(nh):
        src = rows[min(h - 1, int(y / scale))]
        row = bytearray()
        for x in range(nw):
            off = min(w - 1, int(x / scale)) * 3
            row += src[off:off + 3]
        out.append(bytes(row))
    return png_encode_rgb(nw, nh, out)


def crop_png(data, box):
    """Crop to (x, y, w, h), clamped. Original bytes on bad input."""
    try:
        decoded = png_decode_rgb(data)
    except ValueError:
        return data
    if decoded is None:
        return data
    w, h, rows = decoded
    try:
        x, y, cw, chh = (int(v) for v in box)
    except (TypeError, ValueError):
        return data
    x, y = max(0, x), max(0, y)
    cw, chh = min(cw, w - x), min(chh, h - y)
    if cw < 1 or chh < 1 or x >= w or y >= h:
        return data
    out = [r[x * 3:(x + cw) * 3] for r in rows[y:y + chh]]
    return png_encode_rgb(cw, chh, out)


def _shot_pair_finding(key, before_doc, after_doc, before_path,
                       after_path):
    """Screenshot-pair finding, or None when pixels are identical."""
    route = _route_of(key, after_doc) or _route_of(key, before_doc)
    viewport = _viewport_of(key, after_doc) or _viewport_of(
        key, before_doc)
    try:
        with open(before_path, "rb") as f:
            before_bytes = f.read()
        with open(after_path, "rb") as f:
            after_bytes = f.read()
    except OSError as exc:
        return _finding("info", "screenshot unreadable", route,
                        viewport, f"{exc}".strip()[:160])
    regions = diff_regions(before_bytes, after_bytes)
    if not regions["changed"]:
        return None
    before_size = png_size(before_bytes)
    if before_size is not None and regions.get("size") is not None \
            and before_size != regions["size"]:
        return _finding(
            "medium", "screenshot dimensions changed", route,
            viewport,
            f"{before_size[0]}x{before_size[1]} -> "
            f"{regions['size'][0]}x{regions['size'][1]}")
    cells = regions.get("cells") or []
    if not regions.get("exact"):
        return _finding(
            "low", "screenshot changed (undecodable PNG)", route,
            viewport, f"before={before_path} after={after_path}")
    shown = ", ".join(f"({x},{y} {w}x{h})"
                      for x, y, w, h in cells[:8])
    if len(cells) > 8:
        shown += f" +{len(cells) - 8} more"
    return _finding(
        "low", "visual change in screenshot", route, viewport,
        f"{len(cells)} region(s), "
        f"{regions['changed_ratio'] * 100:.1f}% of frame"
        + (f": {shown}" if shown else ""))


def diff_versions(before, after):
    """Diff two captures into severity-ranked findings + evidence.

    before/after are collect_capture snapshots (or capture-dir
    paths, collected on the way in). Returns {"findings": [...],
    "summary": {"critical": n, ...}, "compared": n, "added": [...],
    "removed": [...]}. Pure over the snapshots: no td, no model.
    """
    if isinstance(before, str):
        before = collect_capture(before)
    if isinstance(after, str):
        after = collect_capture(after)
    before_res = before.get("results", {})
    after_res = after.get("results", {})
    before_shots = before.get("screenshots", {})
    after_shots = after.get("screenshots", {})
    findings = []
    compared = 0
    for key in sorted(set(before_res) | set(after_res)):
        if key not in before_res:
            route = _route_of(key, after_res[key])
            findings.append(_finding(
                "info", "route captured only in after", route,
                _viewport_of(key, after_res[key]),
                f"key={key}"))
            continue
        if key not in after_res:
            route = _route_of(key, before_res[key])
            findings.append(_finding(
                "medium", "route missing from after capture", route,
                _viewport_of(key, before_res[key]),
                f"key={key}"))
            continue
        compared += 1
        findings.extend(_diff_result_json(
            key, before_res[key], after_res[key]))
        if key in before_shots and key in after_shots:
            hit = _shot_pair_finding(
                key, before_res[key], after_res[key],
                before_shots[key], after_shots[key])
            if hit is not None:
                findings.append(hit)
    lighthouse_hits = lighthouse_diff(
        before.get("lighthouse", {}), after.get("lighthouse", {}))
    findings.extend(lighthouse_hits)
    ranked = rank_findings(findings)
    summary = {sev: 0 for sev in SEVERITIES}
    for hit in ranked:
        summary[hit.get("severity", "info")] = \
            summary.get(hit.get("severity", "info"), 0) + 1
    return {"findings": ranked, "summary": summary,
            "compared": compared,
            "added": sorted(set(after_res) - set(before_res)),
            "removed": sorted(set(before_res) - set(after_res))}


def lighthouse_scores(capture_dir):
    """{name: {category: 0..1}} from lighthouse JSON artifacts.

    Tolerates lhci/lighthouse shapes (categories.<name>.score);
    missing dir or bad files yield {} entries, never raise (only
    a bad dir type raises ValueError).
    """
    if isinstance(capture_dir, dict):
        docs = capture_dir
    else:
        if not isinstance(capture_dir, str):
            raise ValueError(f"bad capture dir: {capture_dir!r}")
        light = os.path.join(capture_dir, LIGHTHOUSE_DIR) \
            if os.path.isdir(os.path.join(
                capture_dir, LIGHTHOUSE_DIR)) else capture_dir
        docs = {}
        if os.path.isdir(light):
            for name in sorted(os.listdir(light)):
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(light, name),
                              encoding="utf-8") as f:
                        docs[name[:-5]] = json.load(f)
                except (OSError, ValueError):
                    continue
    scores = {}
    for name, doc in docs.items():
        cats = doc.get("categories") if isinstance(doc, dict) \
            else None
        if not isinstance(cats, dict):
            continue
        row = {}
        for cat, body in cats.items():
            if not isinstance(body, dict):
                continue
            try:
                score = float(body.get("score"))
            except (TypeError, ValueError):
                continue
            row[str(cat)] = score
        if row:
            scores[str(name)] = row
    return scores


def lighthouse_read(capture_dir):
    """Human-readable lighthouse summary for one capture dir."""
    scores = lighthouse_scores(capture_dir)
    if not scores:
        return {"scores": {}, "text": "no lighthouse artifacts",
                "findings": []}
    lines = []
    for name in sorted(scores):
        cells = " ".join(f"{cat}={val:.2f}" for cat, val in
                         sorted(scores[name].items()))
        lines.append(f"{name}: {cells}")
    return {"scores": scores, "text": "\n".join(lines),
            "findings": []}


def lighthouse_diff(before, after):
    """Findings for lighthouse score drops between two captures.

    before/after are lighthouse doc dicts (collect_capture's
    ["lighthouse"]) or capture dirs. A drop >= 0.25 is high,
    >= 0.10 medium; gains are info-free (silence is the reward).
    """
    before_scores = lighthouse_scores(before)
    after_scores = lighthouse_scores(after)
    out = []
    for name in sorted(set(before_scores) & set(after_scores)):
        for cat in sorted(set(before_scores[name])
                          & set(after_scores[name])):
            delta = after_scores[name][cat] - before_scores[name][cat]
            if delta <= -LIGHTHOUSE_DROP_HIGH:
                sev = "high"
            elif delta <= -LIGHTHOUSE_DROP_MEDIUM:
                sev = "medium"
            else:
                continue
            out.append(_finding(
                sev, f"lighthouse {cat} dropped {delta:+.2f}",
                name, "",
                f"{before_scores[name][cat]:.2f} -> "
                f"{after_scores[name][cat]:.2f}"))
    return out


def format_evidence_for_model(question, diff_result, limit=6000):
    """Render a diff result as capped text evidence for ask_diff."""
    findings = diff_result.get("findings", [])
    summary = diff_result.get("summary", {})
    lines = [f"question: {question}",
             f"compared {diff_result.get('compared', 0)} "
             f"route+viewport pair(s); "
             + ", ".join(f"{sev}={summary.get(sev, 0)}"
                         for sev in SEVERITIES)]
    for hit in findings[:40]:
        where = hit.get("route", "")
        if hit.get("viewport"):
            where += f" [{hit['viewport']}]"
        lines.append(f"- [{hit.get('severity')}] "
                     f"{hit.get('title')} ({where}): "
                     f"{hit.get('evidence', '')}"[:400])
    if len(findings) > 40:
        lines.append(f"... +{len(findings) - 40} more findings")
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit] + "\n[evidence truncated]"
    return text


def build_diff_content(question, evidence_text, images=None):
    """LangChain-free content blocks for the ask_diff model turn.

    Returns a [{type: text|image_url, ...}] list (OpenAI shape,
    which the Ollama and Nebius chat models both accept). images
    are PNG
    bytes, already downscaled/cropped by the caller — this
    builder never sees full-res dumps. Pure: no langchain import.
    """
    images = images or []
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if not isinstance(evidence_text, str) or not evidence_text.strip():
        raise ValueError("evidence_text must be a non-empty string")
    parts = [{"type": "text",
              "text": f"{evidence_text}\n\nquestion: "
                      f"{question.strip()}"}]
    for png in images:
        if not isinstance(png, (bytes, bytearray)) or not png:
            continue
        parts.append({
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,"
                       + base64.b64encode(bytes(png)).decode()}})
    return parts


ASK_SYSTEM = (
    "You are a UI impact analyst. Answer ONLY from the captured "
    "evidence in this turn: ranked diff findings plus the attached "
    "screenshot crops. Reply with a severity-ranked verdict: what "
    "visually changed, which findings matter, and which are noise. "
    "Cite finding titles verbatim. No repo access, no tools."
)


def ask_diff(question, evidence_text, images=None, prefer=None,
             _model=None):
    """Interrogate diff evidence with a local model. Venv-only path.

    Model comes from deep_chat.build_model (Ollama first, Nebius
    fallback — never Gemini); on a retryable cloud error the call
    gets deep_chat's one local retry. _model injects a stub with
    .invoke(messages) for hermetic tests. Returns the answer text,
    or "ASK_ERROR: ..." when no model is available. Raises
    ValueError on bad question/evidence.
    """
    content = build_diff_content(question, evidence_text,
                                 images=images)
    if _model is None:
        try:
            import deep_chat  # noqa: E402
        except ImportError as exc:
            return f"ASK_ERROR: chat backend not installed: {exc}"
        try:
            model, spec = deep_chat.build_model(prefer=prefer)
        except (RuntimeError, ValueError) as exc:
            return f"ASK_ERROR: no model available: {exc}"
        try:
            from langchain_core.messages import (  # noqa: E402
                HumanMessage, SystemMessage)
        except ImportError as exc:
            return f"ASK_ERROR: chat backend not installed: {exc}"
        messages = [SystemMessage(content=ASK_SYSTEM),
                    HumanMessage(content=content)]
        try:
            answer = deep_chat._invoke_with_failover(
                model, spec, messages)[0]
        except Exception as exc:
            return f"ASK_ERROR: model call failed: {exc}"[:400]
    else:
        messages = content
        try:
            answer = _model.invoke(messages)
        except Exception as exc:
            return f"ASK_ERROR: model call failed: {exc}"[:400]
    text = getattr(answer, "content", answer)
    if isinstance(text, list):
        text = " ".join(str(p.get("text", "")) if isinstance(p, dict)
                        else str(p) for p in text)
    text = str(text or "").strip()
    return text or "ASK_ERROR: empty answer"


def format_diff_text(diff_result, limit=3000):
    """Render a diff result as capped text for tool replies."""
    findings = diff_result.get("findings", [])
    summary = diff_result.get("summary", {})
    head = (f"compared {diff_result.get('compared', 0)} pair(s): "
            + ", ".join(f"{sev}={summary.get(sev, 0)}"
                        for sev in SEVERITIES))
    if not findings:
        return head + "\nno findings"
    lines = [head]
    for hit in findings:
        where = hit.get("route", "")
        if hit.get("viewport"):
            where += f" [{hit['viewport']}]"
        lines.append(f"[{hit.get('severity')}] {hit.get('title')} "
                     f"({where}): {hit.get('evidence', '')}"[:300])
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit] + "\n[truncated]"
    return text


def changed_crops(before_snap, after_snap, max_images=2,
                max_dim=ASK_MAX_DIM):
    """Downscaled crops of changed screenshot regions for ask_diff.

    One crop per changed pair (first changed cell, padded), newest
    keys first, capped at max_images. Never raises on image
    content: unreadable pairs are skipped, so the model turn still
    runs on text evidence.
    """
    try:
        count = max(0, min(4, int(max_images)))
    except (TypeError, ValueError):
        count = 2
    if count == 0:
        return []
    before_shots = before_snap.get("screenshots", {})
    after_shots = after_snap.get("screenshots", {})
    out = []
    for key in sorted(set(before_shots) & set(after_shots)):
        if len(out) >= count:
            break
        try:
            with open(before_shots[key], "rb") as f:
                before_bytes = f.read()
            with open(after_shots[key], "rb") as f:
                after_bytes = f.read()
        except OSError:
            continue
        try:
            regions = diff_regions(before_bytes, after_bytes)
        except ValueError:
            continue
        cells = regions.get("cells") if regions.get("changed") \
            else []
        if not cells:
            continue
        x, y, w, h = cells[0]
        pad = 16
        crop = crop_png(after_bytes,
                        (x - pad, y - pad, w + 2 * pad,
                         h + 2 * pad))
        out.append(downscale_png(crop, max_dim))
    return out


def resolve_capture_dir(checkout, path):
    """Realpath of path when inside checkout or the tmp dir, else None.

    Tool-facing guard: agents pass before/after capture dirs around,
    and those must not become arbitrary-filesystem reads.
    """
    if not isinstance(path, str) or not path:
        return None
    roots = [os.path.realpath(checkout),
             os.path.realpath(tempfile.gettempdir())]
    full = os.path.realpath(os.path.join(checkout, path))
    if not os.path.isdir(full):
        return None
    for root in roots:
        if full == root or full.startswith(root + os.sep):
            return full
    return None


def main(argv=None):
    """Thin CLI for runbook-driven impact passes. Returns exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in (
            "capture", "snapshot", "diff", "ask", "lighthouse"):
        return cli.usage_error(
            "usage: impact.py capture <checkout> [--route R|--all-routes]"
            " [--desktop|--mobile] [--screenshots-only]",
            "       impact.py snapshot <checkout> <label>",
            "       impact.py diff <before-dir> <after-dir>",
            "       impact.py ask <question> <diff-json>",
            "       impact.py lighthouse <capture-dir>")
    verb = argv[0]
    try:
        if verb == "capture":
            if len(argv) < 2:
                return cli.usage_error("capture needs a checkout")
            route, flags = "", argv[2:]
            if "--route" in flags:
                idx = flags.index("--route")
                if idx + 1 >= len(flags):
                    return cli.usage_error("capture: --route needs a value")
                route = flags[idx + 1]
            result = run_ux_capture(
                argv[1], route=route,
                all_routes="--all-routes" in flags,
                desktop="--desktop" in flags,
                mobile="--mobile" in flags,
                screenshots_only="--screenshots-only" in flags)
            print(json.dumps(result, indent=2))
            return 0 if result.get("ok") else 1
        if verb == "snapshot":
            if len(argv) != 3:
                return cli.usage_error(
                    "snapshot needs a checkout and a label")
            print(snapshot_capture(argv[1], argv[2]))
            return 0
        if verb == "diff":
            if len(argv) != 3:
                return cli.usage_error("diff needs before + after dirs")
            print(json.dumps(diff_versions(argv[1], argv[2]), indent=2))
            return 0
        if verb == "ask":
            if len(argv) != 3:
                return cli.usage_error(
                    "ask needs a question and a diff-json file")
            with open(argv[2], encoding="utf-8") as f:
                diff_result = json.load(f)
            print(ask_diff(argv[1], format_evidence_for_model(
                argv[1], diff_result)))
            return 0
        if len(argv) != 2:
            return cli.usage_error("lighthouse needs a capture dir")
        print(json.dumps(lighthouse_read(argv[1]), indent=2))
        return 0
    except ValueError as exc:
        print(f"impact: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"impact: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
