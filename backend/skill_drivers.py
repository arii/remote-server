"""Decoupled browser drivers (issue #1259). One interface.

Recorder (#1252) and replayer (#1255) call the BrowserDriver
interface, never a driver directly. Swapping the driver is one
config line: BROWSER_DRIVER=grid|playwright|browser-mcp (default
grid). No model, no network beyond the browser link itself.

Drivers:
- GridCDPDriver: primary on the S5. Attaches to the live shared
  Grid session (agent session first, else viewer-shared — the same
  attach-only rule as the drive API, never mints a session).
  One attach per call; sequential replay keeps this correct.
- PlaywrightDriver: local Chromium via Playwright (lazy import —
  missing package is a clear error, not a silent fallback).
  The laptop demo driver: headed when BROWSER_HEADLESS=0.
- BrowserMCPDriver: laptop browser via the Browser MCP relay
  (BROWSER_MCP_URL, default http://127.0.0.1:9010) using the
  standard MCP streamable-HTTP tools/call envelope. Snapshot
  mapping and coordinate clicks were written against the public
  BrowserMCP tool surface; the live demo (#1264) is the
  verification — if the paired relay rejects a call, its error
  names the tool and the demo corrects the mapping. The standing
  fallback holds: if the remote link fails, use local Playwright.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from typing import Protocol, runtime_checkable


class BrowserDriverError(Exception):
    """The browser link failed (busy slot, dead relay, no page)."""


@runtime_checkable
class BrowserDriver(Protocol):
    """The recorder/replayer contract. All coordinates are CSS
    pixels in the snapshot's coordinate space."""

    def snapshot(self) -> dict:
        """Current page: {url, title, elements} like the drive API."""
        ...

    def navigate(self, url: str) -> None: ...

    def click(self, x: int, y: int) -> None: ...

    def type_text(self, text: str) -> None: ...

    def key(self, key: str) -> None: ...

    def wheel(self, x: int, y: int, dx: int, dy: int) -> None: ...


def check_driver(driver) -> list:
    """Conformance check for the interface. Empty means conforming."""
    missing = [
        name
        for name in ("snapshot", "navigate", "click", "type_text", "key", "wheel")
        if not callable(getattr(driver, name, None))
    ]
    if not isinstance(driver, BrowserDriver):
        missing.append("protocol")
    return ["missing " + name for name in missing]


DRIVERS = ("grid", "playwright", "browser-mcp")


def get_driver(name: str | None = None):
    """Build the configured driver. One config line swaps it:
    BROWSER_DRIVER=grid|playwright|browser-mcp (default grid)."""
    which = (name or os.environ.get("BROWSER_DRIVER", "grid")).strip()
    if which == "grid":
        return GridCDPDriver()
    if which == "playwright":
        return PlaywrightDriver()
    if which == "browser-mcp":
        return BrowserMCPDriver()
    raise BrowserDriverError(
        f"unknown driver {which!r}; want one of {', '.join(DRIVERS)}"
    )


class GridCDPDriver:
    """Drive the S5 shared Grid session. Attach-only: raises
    BrowserDriverError when no shared session exists."""

    def __init__(self, grid_url: str | None = None):
        base = grid_url or os.environ.get("BROWSER_GRID_URL", "http://browser:4444")
        self.grid_url = base.rstrip("/")

    def _acquire(self):
        from backend.api.routers import browser_view

        session_id = browser_view._grid_live_session()
        if session_id is not None:
            return session_id, None
        claimed = browser_view._viewer_claim()
        if claimed is None:
            raise BrowserDriverError("browser busy: no shared Grid session")
        return claimed, claimed

    def _run(self, fn):
        import contextlib

        from backend.api.routers import browser_view

        session_id, owned = self._acquire()

        async def go():
            bridge = browser_view.CDPBridge(session_id)
            await bridge.connect()
            try:
                return await fn(bridge)
            finally:
                with contextlib.suppress(Exception):
                    await bridge.close()

        try:
            return asyncio.run(go())
        except BrowserDriverError:
            raise
        except Exception as exc:
            raise BrowserDriverError(f"grid call failed: {exc}") from exc
        finally:
            if owned is not None:
                browser_view._viewer_release(owned)

    def snapshot(self) -> dict:
        from backend.api.routers import browser_drive

        async def go(bridge):
            raw = await bridge.call(
                "Runtime.evaluate",
                {"expression": browser_drive.SNAPSHOT_JS, "returnByValue": True},
            )
            value = (raw or {}).get("result", {}).get("value") or {}
            return {
                "url": value.get("url", ""),
                "title": value.get("title", ""),
                "elements": value.get("els", []),
            }

        try:
            return self._run(go)
        except Exception as exc:
            raise BrowserDriverError(f"snapshot failed: {exc}") from exc

    def navigate(self, url: str) -> None:
        async def go(bridge):
            await bridge.navigate(url)

        self._run(go)

    def click(self, x: int, y: int) -> None:
        async def go(bridge):
            await bridge.click(x, y)

        self._run(go)

    def type_text(self, text: str) -> None:
        async def go(bridge):
            await bridge.type_text(text)

        self._run(go)

    def key(self, key: str) -> None:
        async def go(bridge):
            await bridge.key(key, key)

        self._run(go)

    def wheel(self, x: int, y: int, dx: int, dy: int) -> None:
        async def go(bridge):
            await bridge.wheel(x, y, dx, dy)

        self._run(go)


class PlaywrightDriver:
    """Local Chromium via Playwright. Headed when BROWSER_HEADLESS=0."""

    def __init__(self, headless: bool | None = None):
        if headless is None:
            headless = os.environ.get("BROWSER_HEADLESS", "1") != "0"
        self.headless = headless
        self._pw = None
        self._browser = None
        self._page = None

    def _ensure(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserDriverError(
                "playwright not installed: pip install playwright "
                "&& playwright install chromium"
            ) from exc
        from backend.api.routers import browser_drive

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._page = self._browser.new_page()
        self._snapshot_js = browser_drive.SNAPSHOT_JS
        return self._page

    def close(self) -> None:
        for handle in (self._browser, self._pw):
            if handle is not None:
                try:
                    if hasattr(handle, "close"):
                        handle.close()
                    else:
                        handle.stop()
                except Exception:
                    pass
        self._browser = self._pw = self._page = None

    def snapshot(self) -> dict:
        page = self._ensure()
        try:
            value = page.evaluate(self._snapshot_js)
        except Exception as exc:
            raise BrowserDriverError(f"snapshot failed: {exc}") from exc
        return {
            "url": value.get("url", ""),
            "title": value.get("title", ""),
            "elements": value.get("els", []),
        }

    def navigate(self, url: str) -> None:
        try:
            self._ensure().goto(url)
        except Exception as exc:
            raise BrowserDriverError(f"navigate failed: {exc}") from exc

    def click(self, x: int, y: int) -> None:
        try:
            self._ensure().mouse.click(x, y)
        except Exception as exc:
            raise BrowserDriverError(f"click failed: {exc}") from exc

    def type_text(self, text: str) -> None:
        try:
            self._ensure().keyboard.type(text)
        except Exception as exc:
            raise BrowserDriverError(f"type failed: {exc}") from exc

    def key(self, key: str) -> None:
        try:
            self._ensure().keyboard.press(key)
        except Exception as exc:
            raise BrowserDriverError(f"key failed: {exc}") from exc

    def wheel(self, x: int, y: int, dx: int, dy: int) -> None:
        try:
            page = self._ensure()
            page.mouse.move(x, y)
            page.mouse.wheel(dx, dy)
        except Exception as exc:
            raise BrowserDriverError(f"wheel failed: {exc}") from exc


class BrowserMCPDriver:
    """Laptop browser via the Browser MCP relay.

    Calls use the standard MCP streamable-HTTP tools/call envelope
    with the public BrowserMCP tool names. Snapshot elements carry
    refs; coordinate clicks pass x/y through to browser_click.
    Relay errors propagate as BrowserDriverError naming the tool —
    the live demo (#1264) corrects any mapping drift. If the link
    itself is down, use the local Playwright driver instead.
    """

    def __init__(self, relay_url: str | None = None):
        base = relay_url or os.environ.get("BROWSER_MCP_URL", "http://127.0.0.1:9010")
        self.relay_url = base.rstrip("/")
        self._ref_table: dict = {}

    def _call(self, tool: str, args: dict):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self.relay_url + "/mcp",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read().decode("utf-8", "replace")
                ctype = resp.headers.get("Content-Type", "")
        except Exception as exc:
            raise BrowserDriverError(
                f"relay unreachable at {self.relay_url}: {exc}"
            ) from exc
        if "text/event-stream" in ctype:
            data_lines = [
                line[5:] for line in payload.splitlines() if line.startswith("data:")
            ]
            payload = data_lines[-1] if data_lines else ""
        try:
            message = json.loads(payload or "{}")
        except ValueError as exc:
            raise BrowserDriverError(f"{tool}: bad relay response: {exc}") from exc
        if "error" in message:
            raise BrowserDriverError(f"{tool}: {message['error']}")
        result = message.get("result", {})
        content = result.get("content", [])
        if content and isinstance(content[0], dict):
            text = content[0].get("text", "")
            try:
                return json.loads(text)
            except ValueError:
                return text
        return result

    def snapshot(self) -> dict:
        data = self._call("browser_snapshot", {})
        if not isinstance(data, dict):
            raise BrowserDriverError("browser_snapshot: bad shape")
        elements = []
        refs = {}

        def walk(node):
            if not isinstance(node, dict):
                return
            ref = node.get("ref")
            if node.get("role") and ref:
                refs[ref] = node
                elements.append(
                    {
                        "tag": str(node.get("role", "")).upper(),
                        "text": str(node.get("name", "") or "")[:80],
                        "x": 0,
                        "y": 0,
                        "w": 0,
                        "h": 0,
                        "tid": None,
                        "id": None,
                        "name": node.get("name"),
                        "role": node.get("role"),
                        "ref": ref,
                    }
                )
            for child in node.get("children", []) or []:
                walk(child)

        walk(data.get("snapshot", data))
        self._ref_table = refs
        return {
            "url": str(data.get("url", "")),
            "title": str(data.get("title", "")),
            "elements": elements,
        }

    def navigate(self, url: str) -> None:
        self._call("browser_navigate", {"url": url})

    def click(self, x: int, y: int) -> None:
        self._call("browser_click", {"x": x, "y": y})

    def type_text(self, text: str) -> None:
        self._call("browser_type", {"text": text})

    def key(self, key: str) -> None:
        self._call("browser_press_key", {"key": key})

    def wheel(self, x: int, y: int, dx: int, dy: int) -> None:
        self._call("browser_hover", {"x": x, "y": y})
        self._call("browser_wheel", {"x": x, "y": y, "dx": dx, "dy": dy})
