"""Pinned TwT gateway with an audited, model-restricted native Ollama route."""

import argparse
import json as jsonlib
import os
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from twt.catalog import providers
from twt.gateway import Gateway, loopback_origin
from twt.gateway import create_app as hosted_app
from twt.store import Store


class LocalOllama:
    def __init__(self, client, model, base_url):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("S5_TWT_OLLAMA_URL must be a loopback HTTP origin")
        if not model or "cloud" in model.lower():
            raise ValueError("S5_TWT_OLLAMA_MODEL must name an installed local model")
        self.client, self.model = client, model
        self.base_url = base_url.rstrip("/")

    async def validate(self):
        response = await self.client.post(
            self.base_url + "/api/show", json={"model": self.model}
        )
        response.raise_for_status()
        details = response.json()
        if (
            not isinstance(details, dict)
            or details.get("remote_model")
            or details.get("remote_host")
            or not isinstance(details.get("capabilities"), list)
            or "completion" not in details["capabilities"]
        ):
            raise ValueError("Ollama model is not an installed local completion model")
        return details

    async def post(self, url, *, headers, json):
        request = httpx.Request("POST", url)
        if (
            url != self.base_url + "/chat/completions"
            or json.get("model") != self.model
        ):
            raise httpx.RequestError(
                "Local route model or destination rejected", request=request
            )
        try:
            await self.validate()
        except (httpx.HTTPError, ValueError) as error:
            raise httpx.RequestError(
                "Local model validation failed", request=request
            ) from error
        native = {**json["ollama_request"], "model": self.model, "stream": False}
        response = await self.client.post(self.base_url + "/api/chat", json=native)
        if response.status_code >= 400:
            return response
        try:
            payload = response.json()
        except ValueError:
            return response
        if not isinstance(payload, dict) or not isinstance(
            payload.get("message"), dict
        ):
            return response
        message = payload.get("message") or {}
        tools = message.get("tool_calls") or []
        if not isinstance(tools, list) or any(
            not isinstance(call, dict) or not isinstance(call.get("function"), dict)
            for call in tools
        ):
            return response
        calls = [
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {
                    **call["function"],
                    "arguments": jsonlib.dumps(call["function"].get("arguments", {})),
                },
            }
            for index, call in enumerate(tools)
        ]
        completion = {
            "id": "ollama-local",
            "object": "chat.completion",
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": message.get("content"),
                        **({"tool_calls": calls} if calls else {}),
                    },
                    "finish_reason": payload.get("done_reason", "stop"),
                }
            ],
            "usage": {
                "prompt_tokens": payload.get("prompt_eval_count"),
                "completion_tokens": payload.get("eval_count"),
            },
            "ollama_response": payload,
        }
        return httpx.Response(response.status_code, json=completion, request=request)


def create_app(store=None, ollama_client=None, hosted_client=None):
    storage = store or Store()
    native_http = ollama_client or httpx.AsyncClient(timeout=1800, trust_env=False)
    hosted_http = hosted_client or httpx.AsyncClient(timeout=120, trust_env=False)
    model = os.environ.get("S5_TWT_OLLAMA_MODEL", "qwen3:8b")
    local = LocalOllama(
        native_http,
        model,
        os.environ.get("S5_TWT_OLLAMA_URL", "http://127.0.0.1:11434"),
    )
    provider = {
        "id": "ollama-local",
        "base_url": local.base_url,
        "strict": True,
        "trains_on_free_tier": False,
        "free_only": True,
    }
    storage.replace_models(
        "ollama-local",
        [
            {
                "provider": "ollama-local",
                "id": model,
                "chat": True,
                "tools": True,
                "json": True,
                "json_schema": True,
                "free": True,
                "input_price": 0.0,
                "output_price": 0.0,
                "rpm": None,
                "context_length": None,
            }
        ],
    )
    gateway = Gateway(storage, local, [provider])

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if ollama_client is None:
                await native_http.aclose()
            if hosted_client is None:
                await hosted_http.aclose()
            if store is None:
                storage.close()

    app = FastAPI(lifespan=lifespan)

    @app.post("/local/api/chat")
    async def chat(request: Request):
        origin = request.headers.get("origin")
        if origin is not None and not loopback_origin(origin):
            return JSONResponse(
                {"error": "Only loopback origins are allowed"}, status_code=403
            )
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        if (
            not isinstance(body, dict)
            or body.get("model") != model
            or not isinstance(body.get("messages"), list)
            or not body["messages"]
        ):
            return JSONResponse(
                {"error": "Expected the configured local model and nonempty messages"},
                status_code=400,
            )
        trace_id = request.headers.get("X-Trace-Id")
        if trace_id is not None:
            try:
                trace_id = str(uuid.UUID(trace_id))
            except ValueError:
                return JSONResponse(
                    {"error": "X-Trace-Id must be a UUID"}, status_code=400
                )
            if storage.trace(trace_id):
                return JSONResponse({"error": "Trace ID already used"}, status_code=409)
        payload, status, headers = await gateway.complete(
            {
                "model": f"ollama-local/{model}",
                "messages": body["messages"],
                "tools": body.get("tools"),
                "max_tokens": (body.get("options") or {}).get("num_predict", 8192),
                "ollama_request": body,
            },
            "strict",
            request.headers.get("X-Session-Id"),
            trace_id,
            request.is_disconnected,
        )
        if status != 200:
            return JSONResponse(payload, status_code=status, headers=headers)
        native = payload["ollama_response"]
        if body.get("stream", True):
            return Response(
                jsonlib.dumps(native) + "\n",
                media_type="application/x-ndjson",
                headers=headers,
            )
        return JSONResponse(native, headers=headers)

    app.mount("/", hosted_app(storage, hosted_http, providers()))
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
