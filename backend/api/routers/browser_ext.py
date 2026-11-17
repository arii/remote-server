"""Single paired-Chrome transport. Authentication is provided by Buttons ingress."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from backend.api.routers import agent_control, browser_view

router = APIRouter(prefix="/api/browser", tags=["browser_ext"])
logger = logging.getLogger(__name__)
COMMAND_TIMEOUT = 15
_paired: PairedBridge | None = None


class PairedBridge(browser_view.CDPBridge):
    """Reuse CDP input and credential handling, not Grid target attachment."""

    def __init__(self, websocket: WebSocket):
        super().__init__("paired")
        self.ws = websocket
        self.url: str | None = None
        self.title: str | None = None
        self._opened = False
        self._open_lock = asyncio.Lock()

    def disconnect(self, reason: str) -> None:
        self.closed.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError(reason))

    async def request(self, op: str, timeout=None, **payload) -> dict:
        if self.closed.is_set():
            raise ConnectionError("paired Chrome disconnected")
        self._id += 1
        identifier = self._id
        future = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        is_input = payload.get("method", "").startswith("Input.")
        if is_input:
            self._pending_inputs.add(identifier)

        async def exchange():
            await self.ws.send_json({"id": identifier, "op": op, **payload})
            if is_input and self.dialog is not None and not future.done():
                future.set_result({})
            return await future

        try:
            return await asyncio.wait_for(
                exchange(), COMMAND_TIMEOUT if timeout is None else timeout
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"paired Chrome {op} {payload.get('method', '')} timed out"
            ) from exc
        finally:
            self._pending.pop(identifier, None)
            self._pending_inputs.discard(identifier)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def connect(self):
        async with self._open_lock:
            if not self._opened:
                await self.request("open", url="about:blank")
                await self.call("Page.enable")
                self._opened = True
                self.url = "about:blank"

    async def call(self, method, params=None, timeout=None):
        return await self.request("cdp", timeout, method=method, params=params or {})

    async def reattach(self):
        if not self._opened or self.closed.is_set():
            raise ConnectionError("paired Chrome tab closed")

    async def close(self):
        # Per-request release must not tear down the persistent pairing.
        pass

    async def receive(self, message: dict[str, Any]):
        if "id" in message:
            future = self._pending.get(message["id"])
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(
                        RuntimeError(
                            f"paired Chrome command failed: {message['error']}"
                        )
                    )
                else:
                    future.set_result(message.get("result", {}))
            return
        event = message.get("event")
        if event == "human_input" and message.get("kind") in (
            "pointer",
            "key",
            "touch",
        ):
            if agent_control.read_control_state().state != "PAUSED_BY_USER":
                agent_control.pause_agent()
            browser_view._log_activity("takeover", message["kind"], target="paired")
        elif event == "tab_closed":
            self._opened = False
            self.url = self.title = None
            self.dialog = None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("paired Chrome tab closed"))
            browser_view.clear_page("paired")
        elif event == "cdp":
            method = message.get("method")
            params = message.get("params", {})
            if method == "Page.frameNavigated" and not params.get("frame", {}).get(
                "parentId"
            ):
                self._main_frame_id = params["frame"].get("id")
                self.url = params["frame"].get("url")
                self.title = None
                browser_view.remember_url(
                    self.url or "", new_document=True, target="paired"
                )
            elif (
                method == "Page.navigatedWithinDocument"
                and params.get("frameId") == self._main_frame_id
            ):
                self.url = params.get("url")
                browser_view.remember_url(self.url or "", target="paired")
            elif method == "Page.javascriptDialogOpening":
                self.dialog = {key: params.get(key, "") for key in ("type", "message")}
                for identifier in self._pending_inputs:
                    future = self._pending.get(identifier)
                    if future is not None and not future.done():
                        future.set_result({})
            elif method == "Page.javascriptDialogClosed":
                self.dialog = None


def paired_bridge() -> PairedBridge:
    if _paired is None or _paired.closed.is_set():
        raise HTTPException(status_code=409, detail="no paired Chrome")
    return _paired


def paired_state() -> dict:
    bridge = _paired
    if bridge is None or bridge.closed.is_set():
        return {"connected": False, "url": None, "title": None}
    return {
        "connected": True,
        "url": bridge.url,
        "title": bridge.title,
    }


@router.websocket("/ext")
async def extension_socket(websocket: WebSocket):
    global _paired
    if not websocket.headers.get("origin", "").startswith("chrome-extension://"):
        logger.warning(
            "Rejected paired Chrome WebSocket: missing or non-extension Origin"
        )
        await websocket.close(code=1008, reason="extension Origin required")
        return
    await websocket.accept()
    bridge = PairedBridge(websocket)
    previous, _paired = _paired, bridge
    browser_view.clear_page("paired")
    if previous is not None:
        previous.disconnect("paired Chrome replaced by a new connection")
        with contextlib.suppress(Exception):
            await previous.ws.close(code=1000, reason="replaced by a new paired Chrome")
    try:
        while _paired is bridge:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                await websocket.close(code=1008, reason="expected a JSON object")
                break
            await bridge.receive(message)
    except WebSocketDisconnect:
        pass
    finally:
        bridge.disconnect("paired Chrome disconnected")
        if _paired is bridge:
            _paired = None
            browser_view.clear_page("paired")
