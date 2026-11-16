"""Viewer transport, viewport, navigation and physical-key contracts."""

import asyncio
import json
import re
import shutil
import subprocess
import threading
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.routers import browser_view
from backend.tests.test_browser_view import FakeBridge


class ViewerLifecycleTest(unittest.TestCase):
    def test_grid_deletion_closes_even_when_proxy_remains_open(self):
        async def check():
            bridge = browser_view.CDPBridge("dead")

            async def timed_out(waiter, timeout):
                waiter.close()
                raise asyncio.TimeoutError

            with (
                mock.patch.object(
                    browser_view.asyncio, "wait_for", side_effect=timed_out
                ),
                mock.patch.object(
                    browser_view, "_grid_session_alive", side_effect=[True, False]
                ) as alive,
            ):
                await bridge.wait_closed()
            self.assertEqual(
                alive.call_args_list, [mock.call("dead"), mock.call("dead")]
            )
            self.assertTrue(bridge.closed.is_set())

        asyncio.run(check())

    def test_screencast_publishes_initial_actual_url(self):
        bridge = browser_view.CDPBridge("agent")
        bridge._url_cb = mock.AsyncMock()
        bridge.call = mock.AsyncMock(
            side_effect=lambda method, *a, **kw: (
                {"frameTree": {"frame": {"id": "main", "url": "https://example.com/"}}}
                if method == "Page.getFrameTree"
                else {}
            )
        )
        asyncio.run(bridge.start_screencast(mock.AsyncMock()))
        bridge._url_cb.assert_awaited_once_with("https://example.com/")
        self.assertEqual(bridge._main_frame_id, "main")

    def test_viewport_toggle_restarts_frames_and_confirms_view(self):
        bridge = FakeBridge("agent")
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
            mock.patch.object(browser_view, "_grid_live_session", return_value="agent"),
            TestClient(browser_view.create_app()).websocket_connect("/ws") as ws,
        ):
            while ws.receive_json()["type"] != "view":
                pass
            ws.send_json({"t": "viewport", "mode": "mobile"})
            while True:
                message = ws.receive_json()
                if message["type"] == "view":
                    break
            self.assertTrue(message["mobile"])
            self.assertEqual(bridge.calls.count(("screencast",)), 2)
            self.assertFalse(bridge._owns_emulation)

    def test_reader_closes_and_fails_waiters_on_eof_or_bad_message(self):
        async def check(messages):
            bridge = browser_view.CDPBridge("agent")
            bridge.ws = mock.AsyncMock()
            bridge.ws.__aiter__.return_value = messages
            future = asyncio.get_running_loop().create_future()
            bridge._pending[1] = future
            await bridge._reader()
            self.assertTrue(bridge.closed.is_set())
            with self.assertRaises(ConnectionError):
                await future
            with self.assertRaises(ConnectionError):
                await bridge.call("Page.enable")

        for messages in ([], ["invalid json"]):
            asyncio.run(check(messages))

    def test_dead_bridge_closes_viewer_socket_visibly(self):
        bridge = FakeBridge("agent")
        bridge.move = mock.AsyncMock(side_effect=lambda *_: bridge.closed.set())
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
            mock.patch.object(browser_view, "_grid_live_session", return_value="agent"),
            TestClient(browser_view.create_app()).websocket_connect("/ws") as ws,
        ):
            while ws.receive_json()["type"] != "view":
                pass
            ws.send_json({"t": "move", "x": 1, "y": 2})
            self.assertEqual(
                ws.receive_json(), {"type": "error", "msg": "browser disconnected"}
            )
            with self.assertRaises(WebSocketDisconnect) as closed:
                ws.receive_json()
            self.assertEqual(closed.exception.code, 1011)

    def test_websocket_relays_modifier_mask_and_rejects_invalid_mask(self):
        bridge = FakeBridge("agent")
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
            mock.patch.object(browser_view, "_grid_live_session", return_value="agent"),
            mock.patch.object(browser_view, "_pause_agent", return_value=True),
            TestClient(browser_view.create_app()).websocket_connect("/ws") as ws,
        ):
            while ws.receive_json()["type"] != "view":
                pass
            ws.send_json({"t": "key", "key": "a", "code": "KeyA", "modifiers": 2})
            ws.send_json({"t": "key", "key": "x", "modifiers": 99})
            ws.send_json({"t": "key", "key": "x", "modifiers": "invalid"})
            processed = threading.Event()
            bridge.move = mock.AsyncMock(side_effect=lambda *_: processed.set())
            ws.send_json({"t": "move", "x": 0, "y": 0})
            self.assertTrue(processed.wait(5))
            self.assertEqual(bridge.modifiers, 2)
            self.assertEqual(
                [c for c in bridge.calls if c[0] == "key"], [("key", "a", "KeyA")]
            )

    def test_websocket_preserves_altgraph_and_original_modifiers(self):
        bridge = FakeBridge("agent")
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
            mock.patch.object(browser_view, "_grid_live_session", return_value="agent"),
            mock.patch.object(browser_view, "_pause_agent", return_value=True),
            TestClient(browser_view.create_app()).websocket_connect("/ws") as ws,
        ):
            while ws.receive_json()["type"] != "view":
                pass
            processed = threading.Event()
            bridge.move = mock.AsyncMock(side_effect=lambda *_: processed.set())
            ws.send_json(
                {
                    "t": "key",
                    "key": "@",
                    "code": "KeyQ",
                    "modifiers": 3,
                    "altGraph": True,
                }
            )
            ws.send_json({"t": "move", "x": 0, "y": 0})
            self.assertTrue(processed.wait(5))
            self.assertEqual(bridge.modifiers, 3)
            self.assertTrue(bridge.alt_graph)

    def test_control_a_dispatches_chord_not_insert_text(self):
        bridge = browser_view.CDPBridge("agent")
        bridge.ensure_focus = mock.AsyncMock()
        bridge.call = mock.AsyncMock(return_value={})
        asyncio.run(bridge.key("a", "KeyA", 2))
        calls = bridge.call.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c.args[0] == "Input.dispatchKeyEvent" for c in calls))
        self.assertEqual(calls[0].args[1]["windowsVirtualKeyCode"], 65)
        self.assertEqual(calls[0].args[1]["modifiers"], 2)

    def test_reattach_adopts_canonical_phone_viewport(self):
        async def check():
            browser_view._viewer_create("phone", browser_view.MOBILE_VIEWPORT, True)
            bridge = browser_view.CDPBridge("phone")
            bridge.call = mock.AsyncMock(
                side_effect=lambda method, *a, **kw: {
                    "Target.getTargets": {
                        "targetInfos": [{"type": "page", "targetId": "page"}]
                    },
                    "Target.attachToTarget": {"sessionId": "cdp"},
                    "Runtime.evaluate": {"result": {"value": 1}},
                }.get(method, {})
            )
            await bridge.reattach()
            self.assertEqual(bridge.viewport, browser_view.MOBILE_VIEWPORT)
            self.assertTrue(bridge.mobile)
            self.assertFalse(
                any(
                    c.args[0] == "Emulation.setDeviceMetricsOverride"
                    for c in bridge.call.call_args_list
                )
            )
            viewer = browser_view.CDPBridge(
                "phone", viewport=dict(browser_view.VIEWPORT)
            )
            viewer.call = bridge.call
            await viewer.reattach()
            metrics = next(
                c.args[1]
                for c in bridge.call.call_args_list
                if c.args[0] == "Emulation.setDeviceMetricsOverride"
            )
            self.assertEqual((metrics["width"], metrics["height"]), (390, 844))
            bridge.call.reset_mock()
            observer = browser_view.CDPBridge("other-worker")
            observer.call = bridge.call
            await observer.reattach()
            self.assertFalse(
                any(
                    c.args[0] == "Emulation.setDeviceMetricsOverride"
                    for c in observer.call.call_args_list
                )
            )

        with (
            mock.patch.object(browser_view, "_viewer_viewport", None),
            mock.patch.object(browser_view, "_viewer_session_id", None),
            mock.patch.object(browser_view, "_viewer_refcount", 0),
        ):
            asyncio.run(check())

    def test_agent_toggle_is_canonical_after_old_owner_releases(self):
        with (
            mock.patch.object(browser_view, "_viewer_viewport", None),
            mock.patch.object(browser_view, "_viewer_session_id", None),
            mock.patch.object(browser_view, "_viewer_refcount", 0),
        ):
            browser_view._viewer_create("old")
            bridge = browser_view.CDPBridge("agent")
            bridge.reattach = mock.AsyncMock()
            asyncio.run(bridge.set_viewport_mode(True))
            browser_view._viewer_release("old")
            self.assertEqual(
                browser_view._viewer_viewport_for("agent"),
                (browser_view.MOBILE_VIEWPORT, True),
            )

    def test_main_frame_and_same_document_navigation_emit_urls(self):
        async def check():
            bridge = browser_view.CDPBridge("agent")
            bridge._url_cb = mock.AsyncMock()
            for frame in (
                {"id": "main", "url": "https://example.com/"},
                {"id": "child", "parentId": "main", "url": "https://ads.example/"},
                {"id": "main", "url": ""},
            ):
                bridge.on_message(
                    {"method": "Page.frameNavigated", "params": {"frame": frame}}
                )
            bridge.on_message(
                {
                    "method": "Page.navigatedWithinDocument",
                    "params": {"frameId": "main", "url": "https://example.com/#next"},
                }
            )
            bridge.on_message(
                {
                    "method": "Page.navigatedWithinDocument",
                    "params": {"frameId": "child", "url": "https://ads.example/#next"},
                }
            )
            await asyncio.sleep(0)
            self.assertEqual(
                bridge._url_cb.await_args_list,
                [
                    mock.call("https://example.com/"),
                    mock.call("https://example.com/#next"),
                ],
            )

        asyncio.run(check())

    @unittest.skipUnless(shutil.which("node"), "requires Node.js")
    def test_actual_keyboard_handler_and_entire_script_parse(self):
        script = re.search(
            r"<script>([\s\S]*?)</script>", browser_view.VIEWER_HTML
        ).group(1)
        parsed = subprocess.run(
            ["node", "--check"], input=script, text=True, capture_output=True
        )
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        handler = script[
            script.index("document.addEventListener('keydown'") : script.index(
                "// Off-the-shelf"
            )
        ]
        harness = """
const vm = require('node:vm');
let listener, sent = [], connected = true, focused = false;
vm.runInNewContext(HANDLER, {document:{addEventListener:(_, fn)=>listener=fn,
 getElementById:(id)=>({focus:()=>{if(id!=='url') throw Error(id);focused=true}})},
 sendable:()=>connected, sendKey:(...args)=>sent.push(args)});
for(const [key,flags,expected,graph=false] of [['Tab',{},0],['Escape',{},0],['a',{ctrlKey:true},2],
 ['a',{metaKey:true},4],['ArrowLeft',{altKey:true,shiftKey:true},9],['F1',{},0],
 ['@',{ctrlKey:true,altKey:true,getModifierState:()=>true},3,true],
 ['@',{ctrlKey:true,altKey:true,getModifierState:()=>false},3]]) {
 let prevented=false;
 listener({key,code:key,target:{tagName:'BODY'},...flags,preventDefault(){prevented=true}});
 if(!prevented || sent.at(-1)[2]!==expected || sent.at(-1)[3]!==graph) throw Error(key);
}
const count=sent.length;
listener({key:'F6',target:{id:'screen'},preventDefault(){}});
if(!focused || sent.length!==count) throw Error('F6 did not escape remote focus');
for(const e of [{key:'r',ctrlKey:true},{key:'R',metaKey:true,shiftKey:true},
 {key:'l',ctrlKey:true},{key:'l',metaKey:true},{key:'F5'},{key:'F12'},
 {key:'a',target:{id:'url'}},{key:'Enter',target:{tagName:'BUTTON'}},
 {key:' ',target:{tagName:'BUTTON'}},{key:'Backspace',defaultPrevented:true}])
 listener({...e,preventDefault(){throw Error('local key intercepted')}});
connected=false;
listener({key:'a',preventDefault(){throw Error('disconnected input')}});
if(sent.length!==count) throw Error('local/disconnected input forwarded');
""".replace("HANDLER", json.dumps(handler))
        result = subprocess.run(["node", "-e", harness], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
