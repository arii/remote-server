"""CDP dialogs are visible and answerable without WebDriver commands."""

import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from backend.api.routers import browser_drive, browser_view
from backend.tests.test_browser_drive import FakeBridge as DriveBridge, make_client
from backend.tests.test_browser_view import FakeBridge


class DialogTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = mock.patch.dict(
            os.environ,
            {"AGENT_CONTROL_STATE_FILE": os.path.join(directory.name, "control.json")},
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_dialog_delivers_pending_inputs_and_preserves_other_calls(self):
        async def check():
            bridge = browser_view.CDPBridge("test")
            bridge.ws = mock.Mock(send=mock.AsyncMock())
            mouse = asyncio.create_task(bridge.call("Input.dispatchMouseEvent"))
            key = asyncio.create_task(bridge.call("Input.dispatchKeyEvent"))
            page = asyncio.create_task(bridge.call("Page.getFrameTree"))
            await asyncio.sleep(0)
            bridge.on_message(
                {
                    "method": "Page.javascriptDialogOpening",
                    "params": {
                        "type": "prompt",
                        "message": "Question",
                        "defaultPrompt": "private",
                    },
                }
            )
            self.assertEqual(await asyncio.wait_for(mouse, 1), {})
            self.assertEqual(await asyncio.wait_for(key, 1), {})
            self.assertFalse(page.done())
            self.assertEqual(bridge.dialog, {"type": "prompt", "message": "Question"})
            self.assertEqual(
                await asyncio.wait_for(bridge.call("Input.dispatchKeyEvent"), 1), {}
            )
            self.assertFalse(bridge._pending_inputs)
            bridge.on_message({"id": 1, "result": {}})
            bridge.on_message(
                {"method": "Page.javascriptDialogClosed", "params": {"result": True}}
            )
            self.assertIsNone(bridge.dialog)
            bridge.on_message({"id": 3, "result": {"frameTree": {}}})
            self.assertEqual(await page, {"frameTree": {}})
            self.assertFalse(bridge._pending)

        asyncio.run(check())

    def test_input_without_dialog_still_requires_reply_or_times_out(self):
        async def check():
            bridge = browser_view.CDPBridge("test")
            bridge.ws = mock.Mock(send=mock.AsyncMock())
            with self.assertRaises(asyncio.TimeoutError):
                await bridge.call("Input.dispatchMouseEvent", timeout=0.01)
            self.assertIsNone(bridge.dialog)
            self.assertFalse(bridge._pending)
            self.assertFalse(bridge._pending_inputs)
            bridge.ws.send.side_effect = ConnectionError("closed")
            with self.assertRaises(ConnectionError):
                await bridge.call("Input.dispatchMouseEvent")
            self.assertFalse(bridge._pending_inputs)

        asyncio.run(check())

    def test_drive_returns_dialog_without_evaluating_blocked_page(self):
        bridge = DriveBridge("agent")

        async def click(*_):
            bridge.dialog = {"type": "prompt", "message": "Question"}

        bridge.click = click
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
            mock.patch.object(browser_view, "_grid_live_session", return_value="agent"),
            mock.patch.object(browser_drive, "_require_human_not_driving"),
            mock.patch.object(browser_view, "_log_activity"),
            mock.patch.object(
                browser_drive,
                "_snapshot",
                side_effect=AssertionError("must not evaluate an open dialog"),
            ),
        ):
            response = make_client().post(
                "/api/browser/drive", json={"op": "click", "x": 1, "y": 2}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "op": "click",
                "kind": "agent",
                "dialog": {"type": "prompt", "message": "Question"},
            },
        )
        self.assertIn(("close",), bridge.calls)

    def test_attach_enables_page_events_before_runtime_probe(self):
        async def check():
            bridge = browser_view.CDPBridge("test")

            async def call(method, params=None):
                if method == "Target.getTargets":
                    return {"targetInfos": [{"type": "page", "targetId": "main"}]}
                if method == "Target.attachToTarget":
                    return {"sessionId": "page"}
                if method == "Page.enable":
                    return {}
                if method == "Runtime.evaluate":
                    return {"result": {"value": 1}}
                self.fail("unexpected page operation: " + method)

            bridge.call = mock.AsyncMock(side_effect=call)
            await bridge.reattach()
            methods = [call.args[0] for call in bridge.call.await_args_list]
            self.assertLess(
                methods.index("Page.enable"), methods.index("Runtime.evaluate")
            )
            self.assertIsNone(bridge.dialog)

        asyncio.run(check())

    def test_cdp_open_and_close_events(self):
        async def check():
            bridge = browser_view.CDPBridge("test")
            bridge._dialog_cb = mock.AsyncMock()
            for kind, message, default in [
                ("alert", "", ""),
                ("confirm", "Continue?", ""),
                ("prompt", "Name", "default"),
            ]:
                data = {"type": kind, "message": message, "defaultPrompt": default}
                bridge.on_message(
                    {"method": "Page.javascriptDialogOpening", "params": data}
                )
                await asyncio.sleep(0)
                bridge._dialog_cb.assert_awaited_with(data)
            bridge.on_message(
                {"method": "Page.javascriptDialogClosed", "params": {"result": True}}
            )
            await asyncio.sleep(0)
            bridge._dialog_cb.assert_awaited_with(None)

        asyncio.run(check())

    def test_viewer_answers_pause_agent_and_relay_cdp_without_logging_prompt(self):
        for kind, accept, text in [
            ("alert", True, ""),
            ("confirm", False, ""),
            ("prompt", True, "private answer"),
        ]:
            bridge = FakeBridge("agent")
            data = {"type": kind, "message": "Message", "defaultPrompt": "default"}

            async def move(*_):
                await bridge._dialog_cb(data)

            async def call(*_):
                await bridge._dialog_cb(None)
                return {}

            bridge.move = move
            bridge.call = mock.AsyncMock(side_effect=call)
            with (
                mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
                mock.patch.object(
                    browser_view, "_grid_live_session", return_value="agent"
                ),
                mock.patch.object(
                    browser_view, "_pause_agent", return_value=True
                ) as pause,
                mock.patch.object(browser_view, "_log_activity") as log,
                TestClient(browser_view.create_app()).websocket_connect("/ws") as ws,
            ):
                while ws.receive_json()["type"] != "view":
                    pass
                ws.send_json({"t": "move", "x": 0, "y": 0})
                self.assertEqual(ws.receive_json(), {"type": "dialog", "dialog": data})
                ws.send_json({"t": "dialog", "accept": accept, "promptText": text})
                self.assertEqual(ws.receive_json(), {"type": "dialog", "dialog": None})
                bridge.call.assert_awaited_once_with(
                    "Page.handleJavaScriptDialog",
                    {"accept": accept, "promptText": text},
                )
                pause.assert_called_once()
                self.assertEqual(
                    log.call_args.args[:2], ("dialog", "accept" if accept else "cancel")
                )
                self.assertNotIn("private answer", str(log.call_args))

    def test_invalid_answer_does_not_pause_or_dispatch(self):
        bridge = FakeBridge("agent")
        processed = threading.Event()
        bridge.move = mock.AsyncMock(side_effect=lambda *_: processed.set())
        bridge.call = mock.AsyncMock()
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
            mock.patch.object(browser_view, "_grid_live_session", return_value="agent"),
            mock.patch.object(browser_view, "_pause_agent", return_value=True) as pause,
            TestClient(browser_view.create_app()).websocket_connect("/ws") as ws,
        ):
            while ws.receive_json()["type"] != "view":
                pass
            ws.send_json({"t": "dialog", "accept": "false"})
            ws.send_json({"t": "move", "x": 0, "y": 0})
            self.assertTrue(processed.wait(5))
            pause.assert_not_called()
            bridge.call.assert_not_awaited()

    @unittest.skipUnless(shutil.which("node"), "requires Node.js")
    def test_real_modal_script_routes_alert_confirm_prompt_and_disconnect(self):
        script = browser_view.VIEWER_HTML.split("const pageDialog =", 1)[1].split(
            "function setControlsEnabled", 1
        )[0]
        script = "const pageDialog =" + script
        harness = """
const assert=require('node:assert/strict');let connected=true;const sent=[],nodes={};
const document={getElementById(id){return nodes[id]??=( {open:false,events:{},focus(){this.focused=true},showModal(){this.open=true},close(){this.open=false},addEventListener(name,cb){this.events[name]=cb}} )}};
const sendable=()=>connected,ws={send:s=>sent.push(JSON.parse(s))};
SCRIPT
showPageDialog({type:'alert',message:'<unsafe>',defaultPrompt:''});
assert.equal(nodes['dialog-message'].textContent,'<unsafe>');assert.equal(nodes['dialog-cancel'].hidden,true);
answerPageDialog(true);answerPageDialog(true);assert.equal(sent.length,1);assert.equal(pageDialog.open,true);
showPageDialog(null);assert.equal(pageDialog.open,false);
showPageDialog({type:'confirm',message:'Continue?'});assert.equal(nodes['dialog-cancel'].hidden,false);
nodes['dialog-cancel'].events.click();assert.equal(sent.at(-1).accept,false);showPageDialog(null);
showPageDialog({type:'prompt',message:'',defaultPrompt:'default'});
assert.equal(nodes['dialog-prompt'].hidden,false);assert.equal(nodes['dialog-prompt'].value,'default');
nodes['dialog-prompt'].value='typed';nodes['dialog-form'].events.submit({preventDefault(){}});
assert.equal(sent.at(-1).promptText,'typed');showPageDialog(null);
showPageDialog({type:'alert',message:''});pageDialog.events.cancel({preventDefault(){}});assert.equal(sent.at(-1).accept,true);
showPageDialog(null);showPageDialog({type:'confirm',message:''});pageDialog.events.cancel({preventDefault(){}});assert.equal(sent.at(-1).accept,false);
showPageDialog(null);connected=false;showPageDialog({type:'alert',message:''});const count=sent.length;answerPageDialog(true);assert.equal(sent.length,count);showPageDialog(null);
""".replace("SCRIPT", script)
        result = subprocess.run(
            ["node", "-e", harness], text=True, capture_output=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
