import os
import asyncio
import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.shared import model_router
from backend.shared import twt_receipts
from backend.shared import twt_transport

try:
    import twt_service
except ImportError:
    twt_service = None


class TestUDSResolution(unittest.TestCase):
    def test_direct_openai_kwargs_use_configured_socket(self):
        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.dict(os.environ, {"TWT_UDS": root + "/gateway.sock"}),
        ):
            kwargs = model_router.chat_kwargs(
                {"provider": "twt", "model": "default", "base_url": "http://twt/v1"}
            )
            self.assertIn("http_client", kwargs)
            try:
                self.assertEqual(
                    kwargs["http_client"]._transport._pool._uds, root + "/gateway.sock"
                )
            finally:
                kwargs["http_client"].close()
                asyncio.run(kwargs["http_async_client"].aclose())

    def test_host_default_and_explicit_diagnostics_keep_tcp(self):
        with mock.patch.dict(os.environ, {"TWT_UDS": ""}):
            self.assertEqual(twt_transport.client_kwargs(), {})
        with mock.patch.dict(os.environ, {"TWT_UDS": "/run/twt/gateway.sock"}):
            kwargs = model_router.chat_kwargs(
                {
                    "provider": "nebius",
                    "model": "test",
                    "base_url": "https://example.invalid/v1",
                }
            )
            self.assertNotIn("http_client", kwargs)

    def test_bad_socket_path_is_rejected_without_tcp_fallback(self):
        with mock.patch.dict(os.environ, {"TWT_UDS": "relative.sock"}):
            with self.assertRaisesRegex(ValueError, "absolute"):
                twt_transport.client_kwargs()
        import httpx

        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.dict(os.environ, {"TWT_UDS": root + "/missing.sock"}),
        ):
            with httpx.Client(**twt_transport.client_kwargs()) as client:
                with self.assertRaises(httpx.ConnectError):
                    client.get("http://127.0.0.1:8000/v1/models")

    @unittest.skipUnless(
        importlib.util.find_spec("langchain_ollama"), "needs deep-agent venv"
    )
    def test_native_sync_and_async_clients_use_socket(self):
        path = "/run/twt/gateway.sock"
        with mock.patch.dict(os.environ, {"TWT_UDS": path}):
            model, _ = model_router.build_chat_model(
                {
                    "provider": "ollama",
                    "model": "qwen3:8b",
                    "base_url": "http://twt/local",
                }
            )
            try:
                self.assertEqual(model._client._client._transport._pool._uds, path)
                self.assertEqual(
                    model._async_client._client._transport._pool._uds, path
                )
            finally:
                model._client._client.close()
                asyncio.run(model._async_client._client.aclose())

    def test_receipt_fetch_uses_httpx_socket_transport(self):
        import httpx

        with (
            mock.patch.dict(os.environ, {"TWT_UDS": "/run/twt/gateway.sock"}),
            mock.patch.object(httpx, "Client") as factory,
        ):
            client = factory.return_value.__enter__.return_value
            client.post.return_value.json.return_value = {
                "receipts": [{"trace_id": "receipt"}]
            }
            self.assertEqual(
                twt_receipts.fetch("http://twt/v1", ["receipt"]),
                [{"trace_id": "receipt"}],
            )
            self.assertEqual(
                factory.call_args.kwargs["transport"]._pool._uds,
                "/run/twt/gateway.sock",
            )
            client.post.return_value.raise_for_status.assert_called_once()

    def test_only_approved_compose_services_mount_private_socket(self):
        import yaml

        compose = yaml.safe_load(Path("docker-compose.yml").read_text())
        consumers = []
        for name, service in compose["services"].items():
            environment = service.get("environment", {})
            if isinstance(environment, dict) and environment.get("TWT_UDS"):
                consumers.append(name)
                self.assertEqual(
                    service["environment"]["TWT_UDS"], "/run/twt/gateway.sock"
                )
                self.assertIn(
                    "${HOST_HOME:-/home/ari}/twt/run:/run/twt:ro", service["volumes"]
                )
        self.assertEqual(sorted(consumers), ["buttons", "terminal"])

    @unittest.skipUnless(twt_service, "requires pinned TwT")
    def test_socket_permissions_stale_rebind_and_active_socket_rejection(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "private", "gateway.sock")
            with twt_service.bind_uds(path) as first:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                first.listen()
                with self.assertRaisesRegex(ValueError, "active"):
                    twt_service.bind_uds(path)
            with twt_service.bind_uds(path):
                pass
            path.unlink()
            path.write_text("not a socket")
            with self.assertRaisesRegex(ValueError, "non-socket"):
                twt_service.bind_uds(path)
            path.unlink()
            path.parent.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                twt_service.bind_uds(path)

    @unittest.skipUnless(twt_service, "requires pinned TwT")
    def test_one_server_keeps_loopback_and_private_socket(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root, "gateway.sock"))
            with (
                mock.patch(
                    "sys.argv", ["twt_service.py", "--port", "0", "--uds", path]
                ),
                mock.patch.object(twt_service, "create_app"),
                mock.patch.object(twt_service.uvicorn, "Server") as server,
            ):

                def check(*, sockets):
                    self.assertEqual(sockets[0].getsockname()[0], "127.0.0.1")
                    self.assertEqual(sockets[1].getsockname(), path)

                server.return_value.run.side_effect = check
                twt_service.main()
                server.return_value.run.assert_called_once()
            self.assertFalse(Path(path).exists())
