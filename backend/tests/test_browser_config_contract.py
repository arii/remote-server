"""#1260 acceptance: portable browser paths and one configured Grid origin."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from cli import repo_root


ROOT = Path(repo_root()).resolve()
PATH_PROBE = """
import json
from pathlib import Path
from backend import skill_store, skill_runs
from backend.api.routers import browser_view, browser_tasks
paths = {
    "skills": str(skill_store.skills_dir()),
    "notes": str(browser_view.notes_file()),
    "activity": str(browser_view.activity_log_file()),
    "runs": str(skill_runs.runs_file()),
    "tasks": str(browser_tasks.tasks_root()),
}
print(json.dumps({name: str(Path(value).resolve()) for name, value in paths.items()}))
"""


class BrowserContractCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def probe(self, source, overrides=None):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("BROWSER_")
            and key not in ("BUTTONS_STATE_DIR", "S5_CHECKOUT")
        }
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(ROOT), str(ROOT / "deep-agent"))
        )
        environment.update(overrides or {})
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)


class BrowserConfigurationContractTest(BrowserContractCase):
    def test_default_artifacts_resolve_inside_the_checkout(self):
        """#1260: unset path variables use portable repo-relative defaults."""
        paths = self.probe(
            f"import os\nos.chdir({str(self.directory)!r})\n" + PATH_PROBE
        )
        for label, value in paths.items():
            with self.subTest(artifact=label):
                path = Path(value)
                resolved = (ROOT / path).resolve()
                self.assertTrue(
                    resolved.is_relative_to(ROOT),
                    f"{label} defaults outside the checkout: {value}",
                )
                self.assertNotEqual(resolved, ROOT)

    def test_explicit_browser_paths_are_honored(self):
        """#1260 control: every existing artifact override remains usable."""
        names = {
            "skills": "BROWSER_SKILLS_DIR",
            "notes": "BROWSER_NOTES_FILE",
            "activity": "BROWSER_ACTIVITY_FILE",
            "runs": "BROWSER_RUNS_FILE",
            "tasks": "BROWSER_TASKS_DIR",
        }
        expected = {label: str(self.directory / label) for label in names}
        overrides = {variable: expected[label] for label, variable in names.items()}
        self.assertEqual(self.probe(PATH_PROBE, overrides), expected)

    def test_browser_code_contains_no_s5_path_literals(self):
        """#1260's literal grep gate applies to production browser Python."""
        files = set((ROOT / "backend").glob("skill*.py"))
        files.update((ROOT / "backend").glob("*browser*.py"))
        files.update((ROOT / "backend/api/routers").glob("browser*.py"))
        forbidden = (
            "/var/lib/buttons",
            "browser:4444",
            ".s5-shared-browser/agent",
            "/data/shared-browser/agent",
        )
        violations = [
            f"{path.relative_to(ROOT)}:{number}: {literal}"
            for path in sorted(files)
            for number, line in enumerate(path.read_text().splitlines(), 1)
            for literal in forbidden
            if literal in line
        ]
        self.assertEqual(violations, [], "\n".join(violations))

    def test_discovery_and_cdp_use_the_same_configured_grid(self):
        """#1260: real driver discovery and CDP connect share BROWSER_GRID_URL."""
        source = """
        import io
        import json
        from unittest import mock
        from backend.api.routers import browser_view
        from backend.skill_drivers import GridCDPDriver

        requests = []
        status = {"value": {"nodes": [{"slots": [{"session": {
            "sessionId": "contract-session",
            "capabilities": {"boomtick:agentSession": True},
        }}]}]}}
        def respond(request, **kwargs):
            requests.append(request if isinstance(request, str) else request.full_url)
            return io.BytesIO(json.dumps(status).encode())

        socket = mock.MagicMock()
        socket.__aiter__.return_value = []
        socket.close = mock.AsyncMock()
        connect = mock.AsyncMock(return_value=socket)
        snapshot = {"result": {"value": {
            "url": "https://example.test/", "title": "Fixture", "els": [],
        }}}
        with (
            mock.patch("urllib.request.urlopen", side_effect=respond),
            mock.patch("websockets.connect", connect),
            mock.patch.object(browser_view.CDPBridge, "reattach", mock.AsyncMock()),
            mock.patch.object(browser_view.CDPBridge, "call", mock.AsyncMock(return_value=snapshot)),
        ):
            page = GridCDPDriver().snapshot()
        print(json.dumps({"requests": requests, "websocket": connect.await_args.args[0], "page": page}))
        """
        for grid, websocket in (
            (
                "http://grid.example.test:5555/wd/hub/",
                "ws://grid.example.test:5555/wd/hub",
            ),
            (
                "https://grid.example.test:7443/selenium/",
                "wss://grid.example.test:7443/selenium",
            ),
        ):
            with self.subTest(grid=grid):
                observed = self.probe(source, {"BROWSER_GRID_URL": grid})
                self.assertEqual(observed["requests"], [grid.rstrip("/") + "/status"])
                self.assertEqual(
                    observed["websocket"],
                    websocket + "/session/contract-session/se/cdp",
                )
                self.assertEqual(observed["page"]["title"], "Fixture")

    def test_buttons_compose_preserves_existing_s5_artifacts(self):
        """#1260: deployed env keeps the existing S5 volume paths and Grid."""
        docker = shutil.which("docker")
        if docker is None:
            self.skipTest(
                "docker compose is required to resolve deployment configuration"
            )
        version = subprocess.run([docker, "compose", "version"], capture_output=True)
        if version.returncode:
            self.skipTest(
                "docker compose plugin is required to resolve deployment configuration"
            )
        result = subprocess.run(
            [docker, "compose", "--env-file", os.devnull, "config", "--format", "json"],
            cwd=ROOT,
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(self.directory),
                "GITHUB_USER": "contract-probe",
                "TERMINAL_DOMAIN": "example.test",
                "TERMINAL_PORT": "8443",
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        buttons = json.loads(result.stdout)["services"]["buttons"]
        self.assertEqual(
            self.probe(PATH_PROBE, buttons["environment"]),
            {
                "skills": "/var/lib/buttons/skills",
                "notes": "/var/lib/buttons/browser_notes.md",
                "activity": "/var/lib/buttons/browser_activity.jsonl",
                "runs": "/var/lib/buttons/browser_runs.jsonl",
                "tasks": "/var/lib/buttons/browser_tasks",
            },
        )
        self.assertEqual(
            buttons["environment"].get("BROWSER_GRID_URL"), "http://browser:4444"
        )
        self.assertTrue(
            any(
                volume.get("target") == "/var/lib/buttons"
                for volume in buttons["volumes"]
            )
        )
