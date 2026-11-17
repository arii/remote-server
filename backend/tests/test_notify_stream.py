import unittest
from unittest import mock

from starlette.requests import Request

from backend.api.routers import notify as notify_router
from backend.notify import webhook as notify_webhook
from backend.tests.test_notify import _sig


class StreamingWebhookTest(unittest.IsolatedAsyncioTestCase):
    async def send_chunks(self, chunks, signature="", limit=None):
        consumed = []
        iterator = iter(chunks)

        async def receive():
            chunk = next(iterator, None)
            if chunk is None:
                return {"type": "http.request", "body": b"", "more_body": False}
            consumed.append(len(chunk))
            return {"type": "http.request", "body": chunk, "more_body": True}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/webhook",
                "headers": [
                    (b"x-hub-signature-256", signature.encode()),
                    (b"x-github-event", b"ping"),
                ],
            },
            receive,
        )
        cap = notify_webhook.WEBHOOK_MAX_BODY if limit is None else limit
        with (
            mock.patch.object(notify_webhook, "webhook_secret", return_value="s3cret"),
            mock.patch.object(notify_webhook, "WEBHOOK_MAX_BODY", cap),
        ):
            response = await notify_router.github_webhook(request)
        return response, request, consumed

    async def test_unsigned_twelve_mib_upload_stops_at_cap(self):
        chunk = b"x" * (1024 * 1024)
        response, request, consumed = await self.send_chunks([chunk] * 12)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            len(consumed), notify_webhook.WEBHOOK_MAX_BODY // len(chunk) + 1
        )
        self.assertFalse(hasattr(request, "_body"))

    async def test_oversized_first_chunk_does_not_read_the_rest(self):
        response, request, consumed = await self.send_chunks(
            [b"12345", b"rest"], limit=4
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(consumed, [5])
        self.assertFalse(hasattr(request, "_body"))

    async def test_split_body_at_cap_preserves_signature(self):
        body = b'{"hello":"world"}'
        response, _, consumed = await self.send_chunks(
            [body[:5], body[5:]], _sig("s3cret", body), limit=len(body)
        )
        self.assertEqual(response, {"ok": True, "msg": "pong"})
        self.assertEqual(sum(consumed), len(body))

    async def test_small_invalid_signature(self):
        response, _, _ = await self.send_chunks([b"{}"], _sig("wrong", b"{}"))
        self.assertEqual(response.status_code, 401)

    async def test_empty_body_signature(self):
        response, _, consumed = await self.send_chunks([], _sig("s3cret", b""))
        self.assertEqual(response, {"ok": True, "msg": "pong"})
        self.assertEqual(consumed, [])
