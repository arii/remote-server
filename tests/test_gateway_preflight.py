import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from backend.shared import model_router
from backend.shared.twt_provenance import (
    TwTPreflightError,
    TwTVCSMismatchError,
    get_installed_provenance,
    query_gateway_models,
    query_gateway_provenance,
    verify_gateway_capabilities,
    verify_installed_vcs_sha,
)


class MockTransport(httpx.BaseTransport):
    def __init__(self, handler):
        self.handler = handler

    def handle_request(self, request):
        return self.handler(request)


class TestGatewayCapabilitiesPreflight(unittest.TestCase):
    def test_query_gateway_provenance_endpoint(self):
        def handler(req):
            return httpx.Response(200, json={"data": {"commit_id": "abc"}}, request=req)

        client = httpx.Client(transport=MockTransport(handler))
        data = query_gateway_provenance("http://gateway.test/v1", client=client)
        self.assertEqual(data, {"commit_id": "abc"})

    def test_gateway_unreachable_fails_closed(self):
        def handler(req):
            raise httpx.ConnectError("Connection refused", request=req)

        client = httpx.Client(transport=MockTransport(handler))
        with self.assertRaises(TwTPreflightError) as ctx:
            query_gateway_models("http://gateway.test/v1", client=client)
        self.assertIn("Cannot reach TwT gateway", str(ctx.exception))

    def test_gateway_http_error_fails_closed(self):
        def handler(req):
            return httpx.Response(502, text="Bad Gateway", request=req)

        client = httpx.Client(transport=MockTransport(handler))
        with self.assertRaises(TwTPreflightError) as ctx:
            query_gateway_models("http://gateway.test/v1", client=client)
        self.assertIn("returned HTTP 502", str(ctx.exception))

    def test_gateway_invalid_schema_fails_closed(self):
        def handler(req):
            return httpx.Response(200, json={"error": "none"}, request=req)

        client = httpx.Client(transport=MockTransport(handler))
        with self.assertRaises(TwTPreflightError) as ctx:
            query_gateway_models("http://gateway.test/v1", client=client)
        self.assertIn("returned invalid schema", str(ctx.exception))

    def test_stale_gateway_serving_nano_rejects_glm_capability(self):
        # Reproduction of issue #1366: stale gateway has Nemotron Nano but lacks GLM-5.3-Flash
        def handler(req):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "nebius/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                            "owned_by": "nebius",
                        },
                        {
                            "id": "nvidia/google/gemma-3-12b-it",
                            "owned_by": "nvidia",
                        },
                    ],
                },
                request=req,
            )

        client = httpx.Client(transport=MockTransport(handler))
        with self.assertRaises(TwTPreflightError) as ctx:
            verify_gateway_capabilities(
                base_url="http://gateway.test/v1",
                provider_models={"nebius": "zai-org/GLM-5.3-Flash"},
                client=client,
            )
        self.assertIn("TwT gateway capability preflight failed", str(ctx.exception))
        self.assertIn("nebius=zai-org/GLM-5.3-Flash", str(ctx.exception))

    def test_gateway_with_requested_capabilities_passes(self):
        def handler(req):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "nebius/zai-org/GLM-5.3-Flash",
                            "owned_by": "nebius",
                        },
                        {
                            "id": "nvidia/z-ai/glm-5.3-flash",
                            "owned_by": "nvidia",
                        },
                    ],
                },
                request=req,
            )

        client = httpx.Client(transport=MockTransport(handler))
        models = verify_gateway_capabilities(
            base_url="http://gateway.test/v1",
            provider_models={
                "nvidia": "z-ai/glm-5.3-flash",
                "nebius": "zai-org/GLM-5.3-Flash",
            },
            client=client,
        )
        self.assertEqual(len(models), 2)

    def test_provenance_sha_mismatch_fails_closed(self):
        def handler(req):
            if req.url.path == "/v1/models":
                return httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "data": [
                            {"id": "nebius/zai-org/GLM-5.3-Flash", "owned_by": "nebius"}
                        ],
                    },
                    request=req,
                )
            if req.url.path == "/v1/provenance":
                return httpx.Response(
                    200,
                    json={
                        "object": "provenance",
                        "data": {
                            "package": "tokens-with-taste",
                            "commit_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        },
                    },
                    request=req,
                )
            return httpx.Response(404, request=req)

        client = httpx.Client(transport=MockTransport(handler))
        with self.assertRaises(TwTVCSMismatchError) as ctx:
            verify_gateway_capabilities(
                base_url="http://gateway.test/v1",
                provider_models={"nebius": "zai-org/GLM-5.3-Flash"},
                expected_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                client=client,
            )
        self.assertIn("provenance preflight failed", str(ctx.exception))
        self.assertIn("aaaaaaaa", str(ctx.exception))
        self.assertIn("bbbbbbbb", str(ctx.exception))

    def test_provenance_sha_match_passes(self):
        def handler(req):
            if req.url.path == "/v1/models":
                return httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "data": [
                            {"id": "nebius/zai-org/GLM-5.3-Flash", "owned_by": "nebius"}
                        ],
                    },
                    request=req,
                )
            if req.url.path == "/v1/provenance":
                return httpx.Response(
                    200,
                    json={
                        "object": "provenance",
                        "data": {
                            "package": "tokens-with-taste",
                            "commit_id": "c2ddafcdca38f553f9ad1a1473be67bc4b91f825",
                        },
                    },
                    request=req,
                )
            return httpx.Response(404, request=req)

        client = httpx.Client(transport=MockTransport(handler))
        models = verify_gateway_capabilities(
            base_url="http://gateway.test/v1",
            provider_models={"nebius": "zai-org/GLM-5.3-Flash"},
            expected_sha="c2ddafcdca38f553f9ad1a1473be67bc4b91f825",
            client=client,
        )
        self.assertEqual(len(models), 1)


class TestVCSProvenanceVerification(unittest.TestCase):
    def test_get_installed_provenance_nonexistent(self):
        self.assertIsNone(get_installed_provenance("nonexistent-pkg-xyz-123"))

    def test_missing_package_fails_closed(self):
        with self.assertRaises(TwTVCSMismatchError) as ctx:
            verify_installed_vcs_sha("nonexistent-package-xyz", "1234567")
        self.assertIn("is not installed", str(ctx.exception))

    def test_missing_vcs_direct_url_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dist_dir = Path(tmpdir) / "pkg-0.1.0.dist-info"
            dist_dir.mkdir()
            (dist_dir / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: pkg\nVersion: 0.1.0\n"
            )

            with self.assertRaises(TwTVCSMismatchError) as ctx:
                verify_installed_vcs_sha("pkg", "1234567", dist_info_dir=dist_dir)
            self.assertIn("lacks VCS provenance", str(ctx.exception))

    def test_sha_mismatch_aborts_clearly_without_declaring_sha_installed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dist_dir = Path(tmpdir) / "tokens_with_taste-0.1.0.dist-info"
            dist_dir.mkdir()
            (dist_dir / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: tokens-with-taste\nVersion: 0.1.0\n"
            )
            (dist_dir / "direct_url.json").write_text(
                '{"url": "https://github.com/arii/boomtick.git", "vcs_info": {"commit_id": "1111111111111111111111111111111111111111", "vcs": "git"}}'
            )

            with self.assertRaises(TwTVCSMismatchError) as ctx:
                verify_installed_vcs_sha(
                    "tokens-with-taste",
                    "2222222222222222222222222222222222222222",
                    dist_info_dir=dist_dir,
                )
            self.assertIn("VCS SHA mismatch", str(ctx.exception))
            self.assertIn(
                "1111111111111111111111111111111111111111", str(ctx.exception)
            )
            self.assertIn(
                "Refusing to declare requested SHA installed", str(ctx.exception)
            )

    def test_matching_sha_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dist_dir = Path(tmpdir) / "tokens_with_taste-0.1.0.dist-info"
            dist_dir.mkdir()
            (dist_dir / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: tokens-with-taste\nVersion: 0.1.0\n"
            )
            sha = "3e970a570aee0a550ec6409de769c201c9a5b900"
            (dist_dir / "direct_url.json").write_text(
                f'{{"url": "https://github.com/arii/boomtick.git", "vcs_info": {{"commit_id": "{sha}", "vcs": "git"}}}}'
            )

            prov = verify_installed_vcs_sha(
                "tokens-with-taste",
                sha[:10],
                dist_info_dir=dist_dir,
            )
            self.assertEqual(prov["commit_id"], sha)
            self.assertEqual(prov["version"], "0.1.0")


class TestUnchangedPackageVersionVCSUpgradeRegression(unittest.TestCase):
    """Regression test: package version remains 0.1.0 across VCS upgrades.

    In #1359, pip retained version 0.1.0 from commit SHA-A when upgrading to SHA-B
    because pyproject.toml version remained 0.1.0 and pip saw the version satisfied.
    This test verifies that:
    1. A stale direct_url.json is caught and rejected rather than declaring SHA-B installed.
    2. Once direct_url.json is updated (simulating --force-reinstall), verification succeeds.
    """

    def test_unchanged_version_vcs_upgrade_abort_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dist_dir = Path(tmpdir) / "tokens_with_taste-0.1.0.dist-info"
            dist_dir.mkdir()
            (dist_dir / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: tokens-with-taste\nVersion: 0.1.0\n"
            )

            sha_old = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            sha_new = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

            # Step 1: Initial install at sha_old with version 0.1.0
            (dist_dir / "direct_url.json").write_text(
                f'{{"url": "https://github.com/arii/boomtick.git", "vcs_info": {{"commit_id": "{sha_old}", "vcs": "git"}}}}'
            )

            # Step 2: Upgrade attempted to sha_new, but package version 0.1.0 caused pip to skip reinstall.
            # Verifier must ABORT clearly on mismatch rather than declaring sha_new installed:
            with self.assertRaises(TwTVCSMismatchError) as ctx:
                verify_installed_vcs_sha(
                    "tokens-with-taste", sha_new, dist_info_dir=dist_dir
                )
            self.assertIn(
                "Refusing to declare requested SHA installed", str(ctx.exception)
            )
            self.assertIn(sha_old, str(ctx.exception))
            self.assertIn(sha_new, str(ctx.exception))

            # Step 3: Pinned force-reinstall is performed, updating direct_url.json to sha_new
            (dist_dir / "direct_url.json").write_text(
                f'{{"url": "https://github.com/arii/boomtick.git", "vcs_info": {{"commit_id": "{sha_new}", "vcs": "git"}}}}'
            )

            # Step 4: Verification now succeeds with sha_new
            prov = verify_installed_vcs_sha(
                "tokens-with-taste", sha_new, dist_info_dir=dist_dir
            )
            self.assertEqual(prov["commit_id"], sha_new)
            self.assertEqual(prov["version"], "0.1.0")

    def _gateway(self, provenance_response):
        def handler(req):
            if req.url.path == "/v1/models":
                return httpx.Response(
                    200,
                    json={
                        "object": "list",
                        "data": [{"id": "m", "owned_by": "nebius"}],
                    },
                    request=req,
                )
            if req.url.path == "/v1/provenance":
                return provenance_response(req)
            return httpx.Response(404, request=req)

        return httpx.Client(transport=MockTransport(handler))

    def test_expected_sha_without_gateway_provenance_fails_closed(self):
        client = self._gateway(lambda req: httpx.Response(404, request=req))
        with self.assertRaises(TwTVCSMismatchError) as ctx:
            verify_gateway_capabilities(
                base_url="http://gateway.test/v1",
                expected_sha="c2ddafcdca38f553f9ad1a1473be67bc4b91f825",
                client=client,
            )
        self.assertIn("reported no provenance", str(ctx.exception))

    def test_gateway_provenance_without_vcs_info_fails_closed(self):
        client = self._gateway(
            lambda req: httpx.Response(
                200,
                json={"data": {"package": "tokens-with-taste", "version": "0.1.0"}},
                request=req,
            )
        )
        with self.assertRaises(TwTVCSMismatchError) as ctx:
            verify_gateway_capabilities(
                base_url="http://gateway.test/v1",
                expected_sha="c2ddafcdca38f553f9ad1a1473be67bc4b91f825",
                client=client,
            )
        self.assertIn("lacks VCS provenance", str(ctx.exception))

    def test_gateway_short_sha_prefix_does_not_match(self):
        client = self._gateway(
            lambda req: httpx.Response(
                200,
                json={
                    "data": {"commit_id": "c2ddafcdca38f553f9ad1a1473be67bc4b91f825"}
                },
                request=req,
            )
        )
        with self.assertRaises(TwTVCSMismatchError):
            verify_gateway_capabilities(
                base_url="http://gateway.test/v1", expected_sha="c2d", client=client
            )


class TestModelRouterPreflightIntegration(unittest.TestCase):
    def test_local_provider_skips_gateway_preflight(self):
        # Non-twt providers do not require twt gateway
        spec = {"provider": "ollama", "model": "qwen3:8b"}
        # Must not raise even if no gateway is reachable
        model_router.preflight_gateway(spec=spec, stage="coder")

    def test_model_router_preflight_checks_capabilities(self):
        with (
            mock.patch.dict("os.environ", {"TWT_UDS": ""}),
            mock.patch(
                "backend.shared.twt_provenance.verify_gateway_capabilities"
            ) as mock_verify,
        ):
            spec = {
                "provider": "twt",
                "model": "default",
                "base_url": "http://127.0.0.1:8000/v1",
                "provider_models": {"nebius": "zai-org/GLM-5.3-Flash"},
            }
            model_router.preflight_gateway(spec=spec, expected_sha="abc1234")
            mock_verify.assert_called_once_with(
                base_url="http://127.0.0.1:8000/v1",
                uds=None,
                provider_models={"nebius": "zai-org/GLM-5.3-Flash"},
                expected_sha="abc1234",
            )


if __name__ == "__main__":
    unittest.main()
