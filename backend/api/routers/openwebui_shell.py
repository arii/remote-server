"""Open WebUI subpath shell shim (serves the /webui/ page).

Upstream has no base-path support (official docs: serve at root or a
subdomain; the only subpath PR closed unmerged; v0.13.2 has zero
base-path code — its WEBUI_BASE_URL constant is hardcoded ''), so the
stock app 404s its own router under /webui/ even while every
sub-request succeeds (HTTP 200 page, zero failing fetches, in-app
"404: Not Found"). Literal absolute hrefs (/, /notes, /workspace, ...)
in the rendered DOM bypass SvelteKit's base the same way and
full-navigate out of the subpath.

The app's ONLY subpath-hostile bytes are in its ~14KB HTML shell, so
this shim fetches it from the stock container and rewrites three
things: the SvelteKit `base: ""`, absolute asset refs (/_app/*,
/static/*, /manifest.json), and app-route hrefs (kept under /webui by
an injected link rewriter). API/WS/model calls stay root-absolute
(/api/*, /ws/*, /ollama/*, /openai/*, ...) and already work through
the Caddy root handles; /auth is deliberately NOT rewritten (its form
POSTs need the real OUI route, which the root /auth* handle serves).
No source build, no DNS, no Caddy response surgery.
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

# App routes (SvelteKit src/routes at v0.13.2): the sidebar and logo
# link these as literal absolute hrefs. Auth is excluded on purpose
# (form POSTs must reach the real OUI route via the root /auth*
# handle). Single source of truth for the injected rewriter below.
_OUI_APP_ROUTES = (
    "notes|workspace|c|channels|playground|home|admin|automations"
    "|calendar|folders|s|watch|error"
)

# Runs before the app boots and stays for the session: rewrites
# app-route hrefs as the router renders them so clicks stay
# client-side under /webui instead of full-navigating to root paths
# the gate doesn't serve. Root proxy handles (/_app, /api, /ws,
# /auth, ...) never match the route regex and ride through untouched.
_LINK_REWRITER = (
    """<script>(function(){
var R=/^\\/$|^\\/(%s)(\\/|\\?|#|$)/;
function fix(a){var h=a.getAttribute('href');
if(!h||h.charAt(0)!=='/'||h.charAt(1)==='/'||h.indexOf('/webui/')===0)return;
if(R.test(h))a.setAttribute('href','/webui'+h);}
function sweep(r){var as=r.querySelectorAll?r.querySelectorAll('a[href]'):[];
for(var i=0;i<as.length;i++)fix(as[i]);if(r.tagName==='A')fix(r);}
sweep(document);
new MutationObserver(function(muts){for(var i=0;i<muts.length;i++){var m=muts[i];
if(m.type==='attributes'&&m.attributeName==='href')fix(m.target);
for(var j=0;j<m.addedNodes.length;j++){var n=m.addedNodes[j];
if(n.nodeType===1)sweep(n);}}}).observe(document.documentElement,
{childList:true,subtree:true,attributes:true,attributeFilter:['href']});
})();</script>"""
    % _OUI_APP_ROUTES
)

router = APIRouter(tags=["openwebui"])

_BODY_RE = re.compile(r"(<body[^>]*>)")

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
    # subn with a string repl would eat the rewriter's backslashes;
    # a function repl splices it verbatim.
    out, n_body = _BODY_RE.subn(lambda m: m.group(1) + _LINK_REWRITER, out, count=1)
    if n_base < 1 or n_body < 1:
        raise ValueError(
            "shell bootstrap format drifted (base rewrite applied zero times)"
            if n_base < 1
            else "shell has no <body> tag to hang the link rewriter on"
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
