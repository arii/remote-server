"""Acceptance tests for issue #1558: unblock read-only Google Docs export.

Derived independently from the issue's acceptance criteria and the
pre-fix gdocs-access/gdocs.py on main (which called config() before
parsing any flags, so even `--help` required an OAuth client-secret
file). The implementation author's own tests were not consulted.

Stdlib-only; every google-* import is faked in sys.modules, so the suite
is hermetic: no network, no OAuth, no real credential files, no API
calls. Env vars are pinned to empty strings so a real gdocs-access/.env
on the host cannot leak in (load_env only uses setdefault).

Run from the repo root:
    python3 -m unittest tests.test_gdocs_export_1558
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

# Root-relative literal per repo test conventions (cwd = repo root).
GDOCS_PATH = Path("gdocs-access/gdocs.py")

EXPORT_PAYLOAD = b"hello from the fixture doc\n"
EXPORT_TEXT = "hello from the fixture doc\n"


def _load_gdocs():
    spec = importlib.util.spec_from_file_location("gdocs_under_test", GDOCS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRequest:
    """Stands in for google.auth.transport.requests.Request."""


class _FakeCredentials:
    """Stands in for google.oauth2.credentials.Credentials.

    Starts expired with a refresh token so the export path performs a
    simulated refresh; refresh() mutates only in-memory state, so the
    on-disk token file must stay byte-identical.
    """

    instances = []

    def __init__(self, path, scopes):
        self.path = path
        self.scopes = scopes
        self.expired = True
        self.refresh_token = "fake-refresh-token"
        self.valid = False
        self.refresh_calls = 0

    @classmethod
    def from_authorized_user_file(cls, path, scopes):
        instance = cls(path, scopes)
        cls.instances.append(instance)
        return instance

    def refresh(self, request):
        self.refresh_calls += 1
        self.expired = False
        self.valid = True


def _fake_google_modules(recorded, payload=EXPORT_PAYLOAD):
    """Build fake google.* modules; `recorded` captures Drive calls."""

    class _ExportExecution:
        def execute(self):
            return payload

    class _Files:
        def export(self, fileId, mimeType):
            recorded["fileId"] = fileId
            recorded["mimeType"] = mimeType
            return _ExportExecution()

    class _DriveService:
        def files(self):
            return _Files()

    def build(service_name, version, credentials=None):
        recorded.setdefault("build_calls", []).append((service_name, version))
        return _DriveService()

    class _InstalledAppFlow:
        @classmethod
        def from_client_secrets_file(cls, *args, **kwargs):
            raise AssertionError("no OAuth consent flow may start in these tests")

    mods = {
        "google": types.ModuleType("google"),
        "google.auth": types.ModuleType("google.auth"),
        "google.auth.transport": types.ModuleType("google.auth.transport"),
        "google.auth.transport.requests": types.ModuleType(
            "google.auth.transport.requests"
        ),
        "google.oauth2": types.ModuleType("google.oauth2"),
        "google.oauth2.credentials": types.ModuleType("google.oauth2.credentials"),
        "google_auth_oauthlib": types.ModuleType("google_auth_oauthlib"),
        "google_auth_oauthlib.flow": types.ModuleType("google_auth_oauthlib.flow"),
        "googleapiclient": types.ModuleType("googleapiclient"),
        "googleapiclient.discovery": types.ModuleType("googleapiclient.discovery"),
    }
    mods["google.auth.transport.requests"].Request = _FakeRequest
    mods["google.oauth2.credentials"].Credentials = _FakeCredentials
    mods["google_auth_oauthlib.flow"].InstalledAppFlow = _InstalledAppFlow
    mods["googleapiclient.discovery"].build = build
    return mods


class GdocsExport1558Test(unittest.TestCase):
    def setUp(self):
        _FakeCredentials.instances = []
        self.gdocs = _load_gdocs()
        self.recorded = {}
        fakes = _fake_google_modules(self.recorded)
        self._saved_modules = {name: sys.modules.get(name) for name in fakes}
        sys.modules.update(fakes)
        self.addCleanup(self._restore_modules)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.token_path = Path(self._tmp.name) / "fixture-token.json"
        self.token_bytes = json.dumps(
            {
                "token": "fake-access-token",
                "refresh_token": "fake-refresh-token",
                "token_uri": "https://oauth2.example.invalid/token",
                "client_id": "fake-client-id",
                "client_secret": "fake-client-secret",
                "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
            }
        ).encode("utf-8")
        self.token_path.write_bytes(self.token_bytes)

    def _restore_modules(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _env(self, **overrides):
        # Empty strings beat any real gdocs-access/.env via setdefault.
        base = {
            "GOOGLE_CLIENT_SECRET_FILE": "",
            "GOOGLE_TOKEN_FILE": "token.json",
            "GOOGLE_DOCUMENT_ID": "",
        }
        base.update(overrides)
        return mock.patch.dict(os.environ, base)

    def _run_cli(self, argv):
        out = io.StringIO()
        code = None
        with redirect_stdout(out):
            try:
                self.gdocs.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_top_level_help_works_without_oauth_client_secret(self):
        """#1558 AC3: top-level --help must not require OAuth setup."""
        with self._env():
            code, out = self._run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)
        self.assertIn("export", out)

    def test_export_help_works_without_oauth_client_secret(self):
        """#1558 AC3: export --help must not require OAuth setup."""
        with self._env():
            code, out = self._run_cli(["export", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("--token-file", out)

    def test_export_with_token_file_exports_requested_doc_offline(self):
        """#1558 AC1: export --doc-id X --token-file T requests document X
        as text/plain and prints the decoded text, with no OAuth
        client-secret config; the supplied token file stays unchanged
        even when a refresh is simulated."""
        with self._env():
            code, out = self._run_cli(
                [
                    "export",
                    "--doc-id",
                    "fixture-doc",
                    "--token-file",
                    str(self.token_path),
                ]
            )
        self.assertIn(code, (None, 0))
        self.assertEqual(self.recorded.get("fileId"), "fixture-doc")
        self.assertEqual(self.recorded.get("mimeType"), "text/plain")
        self.assertEqual(out, EXPORT_TEXT)
        self.assertEqual(len(_FakeCredentials.instances), 1)
        self.assertEqual(_FakeCredentials.instances[0].path, str(self.token_path))
        self.assertGreaterEqual(_FakeCredentials.instances[0].refresh_calls, 1)
        self.assertEqual(self.token_path.read_bytes(), self.token_bytes)

    def test_export_with_token_file_reads_doc_id_from_env(self):
        """#1558 AC2: GOOGLE_DOCUMENT_ID is the fallback doc id for
        export --token-file when --doc-id is not given."""
        with self._env(GOOGLE_DOCUMENT_ID="env-fixture-doc"):
            code, out = self._run_cli(["export", "--token-file", str(self.token_path)])
        self.assertIn(code, (None, 0))
        self.assertEqual(self.recorded.get("fileId"), "env-fixture-doc")
        self.assertEqual(self.recorded.get("mimeType"), "text/plain")
        self.assertEqual(out, EXPORT_TEXT)

    def test_export_missing_doc_id_reports_doc_id_not_client_secret(self):
        """#1558 AC2: with --token-file but no doc id from either source,
        the CLI reports the missing doc id, not the missing client
        secret."""
        with self._env():
            code, out = self._run_cli(["export", "--token-file", str(self.token_path)])
        self.assertIsInstance(code, str)
        message = code.lower()
        self.assertIn("missing doc id", message)
        self.assertNotIn("client_secret", message)
        self.assertNotIn("client secret", message)

    def test_auth_url_still_requires_client_secret(self):
        """#1558 AC3 (preserved validation): the normal auth flow keeps
        requiring the OAuth client-secret file."""
        with self._env():
            code, out = self._run_cli(["auth-url"])
        self.assertIsInstance(code, str)
        self.assertIn("GOOGLE_CLIENT_SECRET_FILE", code)

    def test_read_and_append_still_require_client_secret(self):
        """#1558 AC3 (preserved validation): read and append keep
        requiring the OAuth client-secret file."""
        for argv in (
            ["read", "--doc-id", "fixture-doc"],
            ["append", "hello", "--doc-id", "fixture-doc"],
        ):
            with self.subTest(argv=argv), self._env():
                code, out = self._run_cli(argv)
            self.assertIsInstance(code, str)
            self.assertIn("GOOGLE_CLIENT_SECRET_FILE", code)

    def test_export_without_token_file_keeps_local_credential_validation(self):
        """#1558 AC3 (preserved validation): export without --token-file
        still goes through the default local-credential path and keeps
        its existing validation."""
        with self._env():
            code, out = self._run_cli(["export", "--doc-id", "fixture-doc"])
        self.assertIsInstance(code, str)
        self.assertIn("GOOGLE_CLIENT_SECRET_FILE", code)


if __name__ == "__main__":
    unittest.main()
