"""Browser-task reports backed by the shared Jobs run registry."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Literal
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl, field_validator

from backend.api.routers import (
    agent_control,
    browser_drive,
    browser_ext,
    browser_view,
    jobs,
)
from backend.exec import agent_turn

router = APIRouter(prefix="/api/browser/tasks", tags=["browser_tasks"])
_workers: dict[str, asyncio.Task] = {}
TASK_LIMIT = 50
POLL_SECONDS = 0.1


class TaskRequest(BaseModel):
    instructions: str = Field(min_length=1, max_length=20000)
    target: browser_drive.DriveTarget
    start_url: HttpUrl | None = None
    mode: Literal["agent", "check"] = "agent"

    @field_validator("instructions")
    @classmethod
    def nonempty_instructions(cls, value):
        if not value.strip():
            raise ValueError("instructions must not be empty")
        return value


def tasks_root() -> Path:
    return Path(os.environ.get("BROWSER_TASKS_DIR", "/var/lib/buttons/browser_tasks"))


def _directory(task_id: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{12}", task_id) is None:
        raise HTTPException(status_code=404, detail="unknown browser task")
    directory = tasks_root() / task_id
    if directory.is_symlink():
        raise HTTPException(status_code=404, detail="unknown browser task")
    return directory


def _read(task_id: str) -> dict:
    path = _directory(task_id) / "report.json"
    if path.is_symlink():
        raise HTTPException(status_code=404, detail="unknown browser task")
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="unknown browser task") from exc


def _save(report: dict) -> None:
    directory = _directory(report["id"])
    # A task pruned while in flight must not resurrect its artifacts.
    if not directory.is_dir():
        return
    temporary = directory / "report.json.tmp"
    temporary.write_text(json.dumps(report, indent=2))
    temporary.replace(directory / "report.json")


def _reports() -> list[dict]:
    reports = []
    for path in tasks_root().glob("*/report.json"):
        try:
            reports.append(_read(path.parent.name))
        except HTTPException:
            continue
    return sorted(reports, key=lambda report: report["created_at"], reverse=True)


def prune_reports() -> None:
    for report in _reports()[TASK_LIMIT:]:
        shutil.rmtree(_directory(report["id"]))


def _view(report: dict) -> dict:
    run = jobs.registry.get_run(report["id"])
    if report["status"] in ("queued", "running"):
        if run is None or run.get("done"):
            report["status"] = (
                "cancelled" if run and run.get("exit_code") == 130 else "failed"
            )
            report["error"] = (
                "cancelled"
                if report["status"] == "cancelled"
                else "task interrupted before completion"
            )
        elif run.get("cancel_requested"):
            report["status"] = "cancelling"
        elif not run.get("queued"):
            report["status"] = "running"
    return {**report, "queue_position": jobs.registry.queue_position(report["id"])}


class TaskStopped(Exception):
    pass


def _check_stop(task_id: str) -> None:
    run = jobs.registry.get_run(task_id)
    if run is None or run.get("cancel_requested") or run.get("exit_code") == 130:
        raise TaskStopped("cancelled")
    if agent_control.read_control_state().state == "PAUSED_BY_USER":
        raise TaskStopped("paused_by_user")


async def _capture(report: dict, start_url: str | None = None) -> None:
    observation, png = await browser_drive.capture(report["target"], start_url)
    n = len(report["captures"])
    directory = _directory(report["id"])
    if directory.is_dir():
        (directory / f"{n}.png").write_bytes(png)
    report["captures"].append({**observation, "shot": n})
    report.update(observation)
    _save(report)


async def _perform(report: dict) -> None:
    await _capture(report, report["start_url"])
    if report["mode"] == "check":
        return
    _check_stop(report["id"])
    chunks = []
    turn_error = None
    try:
        async with contextlib.aclosing(
            agent_turn.manager.run_turn(
                agent_turn.ordered_providers()[0],
                "",
                agent_turn.TERMINAL_STACK,
                report["session_id"],
                report["instructions"],
                False,
                browser_target=report["target"],
            )
        ) as stream:
            async for chunk in stream:
                _check_stop(report["id"])
                if isinstance(chunk, str):
                    chunks.append(chunk)
        result = agent_turn.manager.pop_result(report["session_id"])
        if result is None or result.get("exit") != 0 or result.get("timed_out"):
            raise RuntimeError(
                "browser agent turn failed"
                + (f": {result.get('stderr', '')[:500]}" if result else "")
            )
    except TaskStopped:
        raise
    except Exception as exc:
        turn_error = exc
    report["summary"] = "".join(chunks)
    _check_stop(report["id"])
    await _capture(report)
    if turn_error is not None:
        raise turn_error


async def _worker(report: dict) -> None:
    task_id = report["id"]
    operation = None
    code = 1
    try:
        if not await asyncio.to_thread(jobs.registry.acquire, task_id):
            raise TaskStopped("cancelled")
        _check_stop(task_id)
        report["status"] = "running"
        _save(report)
        operation = asyncio.create_task(_perform(report))
        heartbeat = time.monotonic()
        while not operation.done():
            _check_stop(task_id)
            await asyncio.wait({operation}, timeout=POLL_SECONDS)
            if time.monotonic() - heartbeat >= 5:
                jobs.registry.append(task_id, "")
                heartbeat = time.monotonic()
        _check_stop(task_id)
        await operation
        report["status"] = "done"
        code = 0
    except TaskStopped as exc:
        report["status"] = str(exc)
        code = 130
    except asyncio.CancelledError:
        jobs.registry.cancel_run(task_id)
        report["status"] = "cancelled"
        code = 130
    except Exception as exc:
        if agent_control.read_control_state().state == "PAUSED_BY_USER":
            report["status"] = "paused_by_user"
            code = 130
        else:
            report["status"] = "failed"
            report["error"] = str(exc)
    finally:
        if operation is not None and not operation.done():
            await agent_turn.manager.abort(report["session_id"])
            operation.cancel()
        if operation is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await operation
        report["finished_at"] = time.time()
        try:
            _save(report)
        finally:
            jobs.registry.append(task_id, f"browser task {report['status']}\n")
            jobs.registry.finish_run(task_id, code)


@router.post("", status_code=202)
async def submit(body: TaskRequest) -> dict:
    if body.target == "paired":
        browser_ext.paired_bridge()
    task_id, error = jobs.registry.start_run(
        "browser-task", repo="", resource=f"browser:{body.target}"
    )
    if error:
        raise HTTPException(status_code=409, detail=error)
    report = {
        "id": task_id,
        "session_id": str(uuid.uuid4()),
        **body.model_dump(mode="json"),
        "status": "queued",
        "created_at": time.time(),
        "finished_at": None,
        "captures": [],
        "summary": "",
    }
    try:
        _directory(task_id).mkdir(parents=True)
        _save(report)
        prune_reports()
        browser_view._log_activity(
            "task", body.instructions[:200], via="agent", target=body.target
        )
        worker = asyncio.create_task(_worker(report))
        _workers[task_id] = worker
        worker.add_done_callback(lambda _task: _workers.pop(task_id, None))
    except Exception:
        jobs.registry.finish_run(task_id, 1)
        raise
    return _view(report)


@router.get("")
async def list_tasks() -> dict:
    return {"tasks": [_view(report) for report in _reports()]}


@router.get("/{task_id}")
async def get_task(task_id: str) -> dict:
    return _view(_read(task_id))


@router.post("/{task_id}/cancel", status_code=202)
async def cancel_task(task_id: str) -> dict:
    report = _read(task_id)
    _, error = jobs.registry.cancel_run(task_id)
    if error:
        raise HTTPException(status_code=409, detail=error)
    return _view(report)


@router.get("/{task_id}/shots/{n}.png")
async def screenshot(task_id: str, n: int):
    report = _read(task_id)
    path = _directory(task_id) / f"{n}.png"
    if n < 0 or n >= len(report["captures"]) or not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail="unknown browser screenshot")
    return FileResponse(path, media_type="image/png")
