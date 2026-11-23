"""Run the root unittest suite without allowing live Google Docs requests."""

import unittest
from contextlib import contextmanager
from unittest import mock
from urllib.parse import urlsplit
from urllib.request import OpenerDirector, Request


@contextmanager
def block_google_docs_requests():
    open_url = OpenerDirector.open
    attempted = False
    message = "Tests must not make requests to docs.google.com"

    def guarded_open(opener, fullurl, *args, **kwargs):
        nonlocal attempted
        url = fullurl.full_url if isinstance(fullurl, Request) else fullurl
        if urlsplit(url).hostname == "docs.google.com":
            attempted = True
            raise AssertionError(message)
        return open_url(opener, fullurl, *args, **kwargs)

    with mock.patch.object(OpenerDirector, "open", guarded_open):
        try:
            yield
        finally:
            if attempted:
                raise AssertionError(message)


if __name__ == "__main__":
    with block_google_docs_requests():
        program = unittest.main(module=None, exit=False)
    raise SystemExit(not program.result.wasSuccessful())
