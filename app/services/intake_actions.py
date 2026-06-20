"""Callback-dashboard action log.

The intake spool (intakes.jsonl) is append-only and authoritative — it holds
the call. This file is a separate append-only log of what *staff did* with
each intake: marked it called back, skipped it, added a note.

Why a separate file: keeps intakes.jsonl pure as the audit trail of who
called and what they said. If we ever need to re-derive status from
scratch we only replay this log. No rewriting of historical records.

Format (one JSON object per line):

    {
      "call_control_id": "v3:...",
      "action": "called_back" | "skipped" | "reopened" | "noted",
      "at": "2026-06-14T08:12:33+00:00",
      "by": "feli",
      "note": null
    }

Latest action per call_control_id wins. The dashboard joins this log onto
the intake list at read time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.hangup_spool import _atomic_append, _resolve_path

logger = logging.getLogger(__name__)


VALID_ACTIONS = {"called_back", "skipped", "reopened", "noted"}


async def append_action(
    *,
    spool_path: str,
    call_control_id: str,
    action: str,
    by: str,
    note: str | None = None,
) -> None:
    """Append a single staff action. Logs and swallows errors so a dashboard
    click can't lose the click — caller surfaces success/failure to the UI."""
    if action not in VALID_ACTIONS:
        raise ValueError(f"invalid action: {action!r}")
    record: dict[str, Any] = {
        "call_control_id": call_control_id,
        "action": action,
        "at": datetime.now(timezone.utc).isoformat(),
        "by": by,
        "note": note,
    }
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    path = _resolve_path(spool_path)
    await asyncio.to_thread(_atomic_append, path, line)
    logger.info(
        "Intake action recorded: call=%s action=%s by=%s -> %s",
        call_control_id,
        action,
        by,
        path,
    )


def _read_lines_sync(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def load_intakes(spool_path: str) -> list[dict[str, Any]]:
    """Read every intake record from the spool. Synchronous — fine for the
    request path since the file is small (one line per call).

    Records are returned newest-first so the dashboard renders the most
    recent call at the top without extra sorting in the template.
    """
    path = Path(os.path.expanduser(spool_path))
    records: list[dict[str, Any]] = []
    for raw in _read_lines_sync(path):
        raw = raw.strip()
        if not raw:
            continue
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError:
            logger.warning("Skipping unparseable intake line: %.120s", raw)
    records.sort(key=lambda r: r.get("ended_at") or "", reverse=True)
    return records


def load_actions(spool_path: str) -> dict[str, dict[str, Any]]:
    """Read the action log and return latest-action-per-call.

    Append-order in the file is wall-clock order; later overrides earlier.
    """
    path = Path(os.path.expanduser(spool_path))
    latest: dict[str, dict[str, Any]] = {}
    for raw in _read_lines_sync(path):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ccid = rec.get("call_control_id")
        if not isinstance(ccid, str):
            continue
        latest[ccid] = rec
    return latest


def status_for(action: dict[str, Any] | None) -> str:
    """Map the latest action to a display status for the dashboard.

    `None` (no action ever taken) and `reopened` both surface as "open" so
    staff can find calls that still need follow-up. `noted` keeps whatever
    the previous status was — but we collapse it to "open" here since the
    action log doesn't carry a status field separately.
    """
    if action is None:
        return "open"
    a = action.get("action")
    if a == "called_back":
        return "called_back"
    if a == "skipped":
        return "skipped"
    return "open"
