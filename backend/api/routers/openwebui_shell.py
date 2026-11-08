"""Open WebUI subpath shell shim (serves the /webui/ page).

Upstream has no base-path support (official docs: serve at root or a
subdomain; the only subpath PR closed unmerged; v0.13.2 has zero
base-path code — its WEBUI_BASE_URL constant is hardcoded ''), so the
stock app 404s its own router under /webui/ even while every
sub-request succeeds (HTTP 200 page, zero failing fetches, in-app
"404: Not Found"). The app's ONLY subpath-hostile bytes are in its
~14KB HTML shell: SvelteKit `base: ""` plus absolute asset refs
(/_app/*, /static/*, /manifest.json). API/WS/model calls stay absolute
(/api/*, /ws/*, /ollama/*, /openai/*, /auth/*, ...) and already work
through the Caddy root handles.

This shim fetches the shell from the stock container and rewrites just
those refs to /webui; Caddy's existing strip_prefix handles do the
rest. No source build, no DNS, no Caddy response surgery.
Fail-closed: if the bootstrap format drifts so the base rewrite
applies zero times (upstream SvelteKit upgrade), 502 loudly instead
of serving a page that renders a silent in-app 404.
"""

from __future__ import annotations

import os
import re
import urllib.request

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["openwebui"])

# Compose-network origin of the stock container (same pattern as
# browser_view.GRID_BASE): same host, no new DNS, no new port.
OUI_ORIGIN = os.environ.get("OPENWEBUI_ORIGIN", "http://open-webui:8080")

# The shell's observed absolute refs (enumerated 2026-11-08 on
# v0.13.2): exactly one `base: ""`, plus asset roots below. Double and
# single quotes both occur in the wild; other absolute paths in the
# shell (none observed) would need a Caddy root handle, not surgery.
_BASE_RE = re.compile(r'base:\s*""')
_ASSET_RE = re.compile(r"""(["'])(/(_app/|static/|manifest\.json))""")


def rewrite_oui_shell(html: str) -> str:
    """Point the OUI shell at /webui. Raises ValueError on drift."""
    out, n_base = _BASE_RE.subn('base: "/webui"', html)
    out = _ASSET_RE.sub(r"\1/webui\2", out)
    if n_base < 1:
        raise ValueError(
            "shell bootstrap format drifted (base rewrite applied zero times)"
        )
    return out


@router.get("/_oui-shell", response_class=HTMLResponse)
def oui_shell() -> HTMLResponse:
    """Serve the /webui/ page with a /webui-based shell (see module doc)."""
    try:
        with urllib.request.urlopen(OUI_ORIGIN + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
    except Exception:
        raise HTTPException(status_code=502, detail="open-webui unreachable")
    try:
        return HTMLResponse(rewrite_oui_shell(html))
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
