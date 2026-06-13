"""After-hours intake spool.

When an inbound (after-hours intake) call ends, the media handler appends one
JSON line here: caller metadata, the chosen language, the full conversation
log, and a compact English summary for the attorneys. Same design rationale
as `hangup_spool.py` — durable, inspectable, and no third-party I/O in the
call-teardown path.

Format (one JSON object per line):

    {
      "call_control_id": "v3:...",
      "from": "+15551234567",
      "to": "+14047774002",
      "language": "en" | "es" | null,
      "started_at": "2026-06-09T22:01:11+00:00",
      "ended_at": "2026-06-09T22:04:02+00:00",
      "summary": "Name: ...\nCallback: ...\n...",   // null if generation failed
      "transcript": [{"role": "assistant"|"user", "content": "..."}, ...]
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.services.hangup_spool import _atomic_append, _resolve_path

logger = logging.getLogger(__name__)


async def append_intake(
    *,
    spool_path: str,
    call_control_id: str,
    from_number: str | None,
    to_number: str | None,
    language: str | None,
    started_at: datetime | None,
    transcript: list[dict],
    summary: str | None,
) -> None:
    """Append one intake record. Logs and swallows errors — losing a record
    must not crash call teardown."""
    record: dict[str, Any] = {
        "call_control_id": call_control_id,
        "from": from_number,
        "to": to_number,
        "language": language,
        "started_at": started_at.isoformat() if started_at else None,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "transcript": transcript,
    }
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    path = _resolve_path(spool_path)
    try:
        await asyncio.to_thread(_atomic_append, path, line)
        logger.info(
            "Intake spooled: call=%s lang=%s turns=%d -> %s",
            call_control_id,
            language,
            len(transcript),
            path,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to append to intake spool %s", path)
