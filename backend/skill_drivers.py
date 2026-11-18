"""Decoupled browser drivers (issue #1259). One interface.

Recorder (#1252) and replayer (#1255) call the BrowserDriver
interface, never a driver directly. Swapping the driver is one
config line: BROWSER_DRIVER=grid|playwright (default
grid). No model, no network beyond the browser link itself.

Drivers:
- GridCDPDriver: primary on the S5. Attaches to the live shared
  Grid session (agent session first, else viewer-shared — the same
  attach-only rule as the drive API, never mints a session).
  One attach per call; sequential replay keeps this correct.
- PlaywrightDriver: local Chromium via Playwright (lazy import —
  missing package is a clear error, not a silent fallback).
  The laptop demo driver: headed when BROWSER_HEADLESS=0.
"""

from __future__ import annotations

import asyncio
import os
from typing import Protocol, runtime_checkable

from backend.browser_safety import BrowserDriverError


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


DRIVERS = ("grid", "playwright")


def get_driver(name: str | None = None):
    """Build the configured driver. One config line swaps it:
    BROWSER_DRIVER=grid|playwright (default grid)."""
    which = (name or os.environ.get("BROWSER_DRIVER", "grid")).strip()
    if which == "grid":
        return GridCDPDriver()
    if which == "playwright":
        return PlaywrightDriver()
    raise BrowserDriverError(
        f"unknown driver {which!r}; want one of {', '.join(DRIVERS)}"
    )


class GridCDPDriver:
    """Drive the S5 shared Grid session. Attach-only: raises
    BrowserDriverError when no shared session exists."""

    def _acquire(self):
        from backend.api.routers import browser_view

        session_id = browser_view._grid_live_session()
        if session_id is not None:
            return session_id, None
        claimed = browser_view._viewer_claim()
        if claimed is None:
            raise BrowserDriverError("browser busy: no shared Grid session")
        return claimed, claimed

    def _run(self, fn, *, mutating=True):
        import contextlib

        from backend.api.routers import browser_drive, browser_view

        owned = None

        async def go():
            bridge = browser_view.CDPBridge(session_id)
            await bridge.connect()
            try:
                if mutating:
                    browser_drive._require_human_not_driving()
                return await fn(bridge)
            finally:
                with contextlib.suppress(Exception):
                    await bridge.close()

        try:
            if mutating:
                browser_drive._require_human_not_driving()
            session_id, owned = self._acquire()
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
            return browser_drive.snapshot_from_cdp(raw)

        try:
            return self._run(go, mutating=False)
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
            return {
                "url": value["url"],
                "title": value["title"],
                "elements": value["els"],
            }
        except Exception as exc:
            raise BrowserDriverError(f"snapshot failed: {exc}") from exc

    def navigate(self, url: str) -> None:
        try:
            self._ensure().goto(url)
        except Exception as exc:
            raise BrowserDriverError(f"navigate failed: {exc}") from exc

    def click(self, x: int, y: int) -> None:
        try:
            page = self._ensure()
            page.mouse.click(x, y)
            page.evaluate("void 0")
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
