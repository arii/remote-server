"""Screencast geometry must survive capture downscaling."""

import json
import re
import shutil
import subprocess
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from backend.api.routers import browser_view
from backend.tests.test_browser_view import FakeBridge


class FrameCoordinatesTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "requires Node.js")
    def test_height_only_frame_resize_updates_canvas_and_bottom_click(self):
        body = re.search(
            r"img.onload = \(\) => \{([\s\S]*?)\n      \};", browser_view.VIEWER_HTML
        ).group(1)
        position = re.search(
            r"function pos\(e\) \{[\s\S]*?\n\}", browser_view.VIEWER_HTML
        ).group(0)
        script = """
const assert=require('node:assert/strict');
const cv={width:1280,height:800,getBoundingClientRect(){return {left:0,top:0,width:this.width,height:this.height}}};
const img={width:1280,height:772},m={metadata:{deviceWidth:1912,deviceHeight:1153}};
let frameSize=null,frames=1;const t0=0,dsf=1,performance={now:()=>0},URL={revokeObjectURL(){}};
const ctx={drawImage(){assert.equal(cv.height,img.height)}};
(()=>{FRAME})();
assert.equal(cv.height,772);assert.equal(cv.width,1280);
POSITION
assert.deepEqual(pos({clientX:1671*1280/1912,clientY:1085*772/1153}),{x:1671,y:1085});
""".replace("FRAME", body).replace("POSITION", position)
        result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_frame_metadata_reaches_viewer(self):
        bridge = FakeBridge("agent")
        metadata = {"deviceWidth": 1600, "deviceHeight": 1000}

        async def start(callback):
            await callback({"data": "image", "metadata": metadata})

        bridge.start_screencast = start
        with (
            mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
            mock.patch.object(browser_view, "_grid_live_session", return_value="agent"),
            TestClient(browser_view.create_app()).websocket_connect("/ws") as ws,
        ):
            while True:
                message = ws.receive_json()
                if message["type"] == "frame":
                    break
            self.assertEqual(message["metadata"], metadata)

    @unittest.skipUnless(shutil.which("node"), "requires Node.js")
    def test_downscaled_and_initial_frame_coordinates(self):
        function = re.search(
            r"function pos\(e\) \{[\s\S]*?\n\}", browser_view.VIEWER_HTML
        ).group(0)
        for dimensions, dsf, size, point, expected in [
            (
                [1280, 800],
                1,
                {"width": 1600, "height": 1000},
                [880, 480],
                {"x": 1100, "y": 600},
            ),
            (
                [2560, 1600],
                2,
                {"width": 1280, "height": 800},
                [500, 300],
                {"x": 500, "y": 300},
            ),
            ([300, 150], 1, None, [100, 50], {"x": 100, "y": 50}),
        ]:
            code = f"""
const [width,height]={json.dumps(dimensions)};
const cv={{width,height,getBoundingClientRect:()=>({{left:0,top:0,width:width/{dsf},height:height/{dsf}}})}};
const dsf={dsf}, frameSize={json.dumps(size)};
{function}
const p=pos({{clientX:{point[0]},clientY:{point[1]}}});
if(JSON.stringify(p)!=={json.dumps(json.dumps(expected, separators=(",", ":")))}) throw Error(JSON.stringify(p));
"""
            result = subprocess.run(
                ["node", "-e", code], text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
