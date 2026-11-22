"""Private TwT transport settings; HTTP dependencies load only for clients."""

import os


def uds_path():
    path = os.environ.get("TWT_UDS", "").strip()
    if path and not os.path.isabs(path):
        raise ValueError("TWT_UDS must be an absolute socket path")
    return path or None


def client_kwargs(*, asynchronous=False, hook=None, uds=None):
    kwargs = {"event_hooks": {"request": [hook]}} if hook else {}
    path = uds or uds_path()
    if path:
        import httpx

        transport = httpx.AsyncHTTPTransport if asynchronous else httpx.HTTPTransport
        kwargs.update(transport=transport(uds=path), trust_env=False)
    return kwargs


def openai_clients():
    import httpx
    from backend.shared import twt_receipts

    return {
        "http_client": httpx.Client(**client_kwargs(hook=twt_receipts.capture)),
        "http_async_client": httpx.AsyncClient(
            **client_kwargs(asynchronous=True, hook=twt_receipts.acapture)
        ),
    }
