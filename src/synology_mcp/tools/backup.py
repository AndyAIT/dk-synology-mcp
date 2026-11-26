"""Backup tools — Hyper Backup task management and monitoring.

Covers: list tasks, task status, start/cancel backup, integrity check,
and backup version info.
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict

from ..utils.formatters import format_size, format_timestamp, handle_synology_error, error_response


class BackupNasInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    nas: Optional[str] = Field(default=None, description="NAS name")


class BackupTaskInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    task_id: int = Field(..., description="Backup task ID")
    nas: Optional[str] = Field(default=None, description="NAS name")


class BackupHealthInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    nas: Optional[str] = Field(default=None, description="NAS name")
    days: int = Field(default=30, ge=1, le=365, description="How many days of history to summarise")


def _last_result(data: dict) -> dict:
    """Reduce a Hyper Backup task result to the fields that matter."""
    return {
        "result": data.get("last_bkp_result"),
        "last_start": data.get("last_bkp_time"),
        "last_end": data.get("last_bkp_end_time"),
        "last_success": data.get("last_bkp_success_time"),
        "next_backup": data.get("next_bkp_time"),
        "error": data.get("last_bkp_error") or None,
        "error_code": data.get("last_bkp_error_code"),
        "state": data.get("state"),
        "status": data.get("status"),
    }


def _fetch_logs(bk, since: int, until: int, page: int = 500, max_pages: int = 20) -> list:
    """Page through Hyper Backup logs for a time window."""
    entries: list = []
    for i in range(max_pages):
        res = bk.hb_logs_get(
            limit=page, offset=i * page, filter_date_from=since, filter_date_to=until
        )
        batch = (res or {}).get("data", {}).get("log_list", []) if isinstance(res, dict) else []
        entries.extend(batch)
        if len(batch) < page:
            break
    return entries


def _summarise_logs(entries: list, task_names: list) -> dict:
    """Group log events per task and count backup / integrity outcomes."""
    per_task = {
        n: {"backups_started": 0, "backups_succeeded": 0, "integrity_ok": 0,
            "integrity_problems": 0, "issues": []}
        for n in task_names
    }
    other_issues = []
    for e in entries:
        text = e.get("event", "")
        level = (e.get("level") or "").lower()
        low = text.lower()
        task = next((n for n in task_names if f"[{n}]" in text), None)
        slot = per_task.get(task)
        issue = {"time": e.get("time"), "level": level, "event": text}
        if slot is None:
            if level != "info":
                other_issues.append(issue)
            continue
        if "backup task started" in low:
            slot["backups_started"] += 1
        elif "backup task finished successfully" in low:
            slot["backups_succeeded"] += 1
        elif "integrity check is finished" in low:
            if "no error was found" in low:
                slot["integrity_ok"] += 1
            else:
                slot["integrity_problems"] += 1
                slot["issues"].append(issue)
        elif level != "info":
            slot["issues"].append(issue)
    return {"tasks": per_task, "other_issues": other_issues}


def register_backup_tools(mcp, conn_mgr) -> None:
    """Register Hyper Backup tools."""

    def _bk(nas=None):
        return conn_mgr.get_client("backup", nas)

    @mcp.tool(
        name="synology_backup_last_result",
        annotations={"title": "Last Backup Result", "readOnlyHint": True, "destructiveHint": False},
    )
    async def synology_backup_last_result(params: BackupTaskInput) -> str:
        """Get the outcome of the last run of a Hyper Backup task (result, times, error)."""
        try:
            bk = _bk(params.nas)
            result = bk.backup_task_result(task_id=params.task_id)
            if not isinstance(result, dict) or "data" not in result:
                return error_response(f"No result for backup task {params.task_id}")
            return json.dumps(_last_result(result["data"]), indent=2, default=str)
        except Exception as e:
            return handle_synology_error(e, "Backup last result")

    @mcp.tool(
        name="synology_backup_health",
        annotations={"title": "Backup Health", "readOnlyHint": True, "destructiveHint": False},
    )
    async def synology_backup_health(params: BackupHealthInput) -> str:
        """Summarise backup health over the last N days: per task, the last result,
        successful runs, integrity-check outcomes and any warnings or errors from the
        Hyper Backup log."""
        import time

        try:
            bk = _bk(params.nas)
            listing = bk.backup_task_list()
            if not isinstance(listing, dict) or "data" not in listing:
                return error_response("Could not list backup tasks")
            tasks = listing["data"].get("task_list", [])
            names = {t.get("task_id"): t.get("name", "") for t in tasks}

            now = int(time.time())
            entries = _fetch_logs(bk, now - params.days * 86400, now)
            summary = _summarise_logs(entries, [n for n in names.values() if n])

            out_tasks = []
            overall = "healthy"
            for tid, name in names.items():
                res = bk.backup_task_result(task_id=tid)
                last = _last_result(res["data"]) if isinstance(res, dict) and "data" in res else {}
                counts = summary["tasks"].get(name, {})
                verdict = "healthy"
                if last.get("result") not in ("done", None) or last.get("error"):
                    verdict = "attention"
                elif counts.get("issues") or counts.get("integrity_problems"):
                    verdict = "attention"
                elif not counts.get("backups_succeeded"):
                    verdict = "no successful backup in window"
                if verdict != "healthy":
                    overall = "attention"
                out_tasks.append({"task_id": tid, "name": name, "verdict": verdict,
                                  "last": last, **counts})
            return json.dumps({
                "window_days": params.days,
                "overall": overall,
                "log_entries_scanned": len(entries),
                "tasks": out_tasks,
                "other_issues": summary["other_issues"],
            }, indent=2, default=str)
        except Exception as e:
            return handle_synology_error(e, "Backup health")

    @mcp.tool(
        name="synology_backup_list",
        annotations={"title": "List Backup Tasks", "readOnlyHint": True, "destructiveHint": False},
    )
    async def synology_backup_list(params: BackupNasInput) -> str:
        """List all Hyper Backup tasks and their status."""
        try:
            bk = _bk(params.nas)
            result = bk.backup_task_list()
            if not result or "data" not in result:
                return error_response("Could not list backup tasks")
            tasks = result["data"].get("task_list", result["data"])
            items = []
            if isinstance(tasks, list):
                for t in tasks:
                    items.append({
                        "id": t.get("task_id", t.get("id", "")),
                        "name": t.get("name", ""),
                        "status": t.get("status", ""),
                        "last_backup": format_timestamp(t.get("last_backup_time")),
                        "next_backup": format_timestamp(t.get("next_backup_time")),
                    })
            return json.dumps({"tasks": items, "count": len(items)}, indent=2, default=str)
        except Exception as e:
            return handle_synology_error(e, "List backups")

    @mcp.tool(
        name="synology_backup_status",
        annotations={"title": "Backup Task Status", "readOnlyHint": True, "destructiveHint": False},
    )
    async def synology_backup_status(params: BackupTaskInput) -> str:
        """Get detailed status of a specific backup task."""
        try:
            bk = _bk(params.nas)
            result = bk.backup_task_status(task_id=params.task_id)
            if not result or "data" not in result:
                return error_response(f"No status for backup task {params.task_id}")
            return json.dumps(result["data"], indent=2, default=str)
        except Exception as e:
            return handle_synology_error(e, "Backup status")

    @mcp.tool(
        name="synology_backup_run",
        annotations={"title": "Start Backup", "readOnlyHint": False, "destructiveHint": False},
    )
    async def synology_backup_run(params: BackupTaskInput) -> str:
        """Trigger an immediate backup for a task."""
        try:
            bk = _bk(params.nas)
            result = bk.backup_task_run(task_id=params.task_id)
            return json.dumps({
                "status": "success",
                "action": "started",
                "task_id": params.task_id,
                "message": "Backup task started. Check status with synology_backup_status.",
            }, indent=2)
        except Exception as e:
            return handle_synology_error(e, "Start backup")

    @mcp.tool(
        name="synology_backup_cancel",
        annotations={"title": "Cancel Backup", "readOnlyHint": False, "destructiveHint": False},
    )
    async def synology_backup_cancel(params: BackupTaskInput) -> str:
        """Cancel a running backup task."""
        try:
            bk = _bk(params.nas)
            result = bk.backup_task_cancel(task_id=params.task_id)
            return json.dumps({"status": "success", "action": "cancelled", "task_id": params.task_id}, indent=2)
        except Exception as e:
            return handle_synology_error(e, "Cancel backup")

    @mcp.tool(
        name="synology_backup_integrity_check",
        annotations={"title": "Backup Integrity Check", "readOnlyHint": False, "destructiveHint": False},
    )
    async def synology_backup_integrity_check(params: BackupTaskInput) -> str:
        """Start an integrity check on a backup task to verify data consistency."""
        try:
            bk = _bk(params.nas)
            result = bk.integrity_check_run(task_id=params.task_id)
            if not isinstance(result, dict) or not result.get("success", "data" in result):
                return error_response("Could not start integrity check")
            return json.dumps(
                {"status": "started", "task_id": params.task_id, **(result.get("data") or {})},
                indent=2,
                default=str,
            )
        except Exception as e:
            return handle_synology_error(e, "Integrity check")
