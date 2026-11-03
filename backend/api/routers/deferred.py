"""deferred router: defer-to-later flags for Review triage.

A deferred flag per issue/PR hides the row from the Review lists and
shows it in a 'Deferred' disclosure with a restore action. Defer is
reversible (no confirm tap, no destructive semantics).

- GET    /api/deferred?repo=            deferred rows for a repo
- POST   /api/deferred                  defer one row
- DELETE /api/deferred                  restore one row

Persists as a JSON file on the buttons-data volume, same pattern as
the audit ledger (backend/ledger/__init__.py): fixed default path,
env override read dynamically so tests can point at a tmp file.
"""

from __future__ import annotations

import json
import os
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core import validate_repo_name

router = APIRouter(prefix="/api", tags=["deferred"])

DEFAULT_DEFERRED_PATH = "/var/lib/buttons/deferred.json"

DEFERRED_KINDS = ("pr", "issue")

_deferred_lock = threading.Lock()


def deferred_path() -> str:
    """Current deferred file path (env override read dynamically so
    tests can point at a tmp dir without import tricks)."""
    return os.environ.get("BUTTONS_DEFERRED_PATH",
                          DEFAULT_DEFERRED_PATH)


class DeferredRequest(BaseModel):
    repo: str = "remote-server"
    kind: str = ""
    number: object = 0


class DeferredRow(BaseModel):
    kind: str = ""
    number: int = 0
    deferred_at: float = 0.0


class DeferredList(BaseModel):
    deferred: list[DeferredRow] = Field(default_factory=list)


def _read_store() -> dict:
    """Whole store {repo: [rows]}. {} on any failure (missing or
    corrupt file reads as empty; a write repairs it)."""
    try:
        with open(deferred_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_store(store: dict) -> None:
    path = deferred_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, sort_keys=True)
    os.replace(tmp, path)


def _clean_rows(rows) -> list:
    """Keep well-formed stored rows only; corrupt entries are dropped
    on read so one bad row can't wedge the list."""
    clean = []
    if not isinstance(rows, list):
        return clean
    for r in rows:
        if (isinstance(r, dict) and r.get("kind") in DEFERRED_KINDS
                and isinstance(r.get("number"), int)
                and not isinstance(r.get("number"), bool)
                and r["number"] > 0):
            clean.append({"kind": r["kind"], "number": r["number"],
                          "deferred_at": r.get("deferred_at", 0.0)})
    return clean


def _validated(body: DeferredRequest) -> tuple[str, str, int]:
    """(repo, kind, number) or raise HTTPException(400)."""
    try:
        name = validate_repo_name(body.repo)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="bad repo")
    if body.kind not in DEFERRED_KINDS:
        raise HTTPException(status_code=400, detail="bad kind")
    if (not isinstance(body.number, int)
            or isinstance(body.number, bool)
            or body.number <= 0):
        raise HTTPException(status_code=400, detail="bad number")
    return name, body.kind, body.number


@router.get("/deferred", response_model=DeferredList)
def list_deferred(repo: str = "remote-server") -> dict:
    try:
        name = validate_repo_name(repo)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="bad repo")
    with _deferred_lock:
        rows = _clean_rows(_read_store().get(name, []))
    return {"deferred": rows}


@router.post("/deferred", response_model=DeferredList)
def defer_row(body: DeferredRequest) -> dict:
    name, kind, number = _validated(body)
    with _deferred_lock:
        store = _read_store()
        rows = _clean_rows(store.get(name, []))
        if not any(r["kind"] == kind and r["number"] == number
                   for r in rows):
            rows.append({"kind": kind, "number": number,
                         "deferred_at": time.time()})
        store[name] = rows
        try:
            _write_store(store)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)[:200])
    return {"deferred": rows}


@router.delete("/deferred", response_model=DeferredList)
def restore_row(body: DeferredRequest) -> dict:
    name, kind, number = _validated(body)
    with _deferred_lock:
        store = _read_store()
        rows = [r for r in _clean_rows(store.get(name, []))
                if not (r["kind"] == kind and r["number"] == number)]
        store[name] = rows
        try:
            _write_store(store)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)[:200])
    return {"deferred": rows}
