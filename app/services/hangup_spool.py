"""Outbound-call hangup feedback spool.

When Telnyx fires `call.hangup` for a call we placed, the webhook handler
appends one JSON line to a local spool file. The dialer CLI
(`tools/dialer.py`) drains the spool into the Google Sheet on the next run.

Why a local file instead of writing to the sheet directly from the webhook:

1. The webhook has to ack Telnyx fast (<10s, ideally <1s). Sheets writes are
   200-800ms and can throttle / 5xx — that latency does not belong in the
   request path, and a Sheets outage should not produce Telnyx retry storms.
2. Feli-Voice has no Google credentials today. Keeping all Sheets I/O in
   `tools/dialer.py` is one place to break, one place to fix.
3. JSONL is durable across restarts and trivially inspectable (`tail -f`).

Format (one JSON object per line):

    {
      "call_control_id": "v3:...",
      "row": 5,
      "sheet_id": "1AbC...",        // optional
      "hangup_cause": "normal_clearing",
      "hangup_source": "callee",
      "duration_secs": 42,
      "ended_at": "2026-06-06T17:42:11-04:00",
      "to": "+15551234567"
    }

Lines without a `row` are still recorded (useful for forensics) but the
dialer skips them when flushing.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _resolve_path(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


def decode_client_state(client_state: str | None) -> dict[str, Any]:
    """Decode the base64(json) blob we set on outbound dial.

    Returns {} if the field is missing, malformed, or doesn't look like ours.
    Never raises — webhook handling must keep going.
    """
    if not client_state:
        return {}
    try:
        raw = base64.b64decode(client_state, validate=False)
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        # Telnyx will sometimes echo whatever string is set, including
        # non-base64 values from other tooling. Silently ignore.
        pass
    return {}


def _atomic_append(path: Path, line: str) -> None:
    """Append a single line, creating parents on demand. Blocking — caller
    runs this off the event loop via asyncio.to_thread."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open in line-buffered mode and flush; one webhook one line, so we
    # don't need fancy locking — POSIX append writes are atomic for sizes
    # below PIPE_BUF (~4KB), and our lines are well under that.
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


async def append_hangup(
    *,
    spool_path: str,
    call_control_id: str,
    client_state: str | None,
    hangup_cause: str | None,
    hangup_source: str | None,
    to: str | None,
    start_time: datetime | None,
    end_time: datetime | None,
) -> None:
    """Append a hangup record to the spool. Safe to call from request path.

    Runs the actual disk write in a thread so we never block the event loop
    on filesystem I/O. Logs and swallows errors — losing a single hangup
    record must not break the webhook ack.
    """
    state = decode_client_state(client_state)
    ended_at = end_time or datetime.now(timezone.utc)
    duration_secs: int | None = None
    if start_time and end_time:
        try:
            duration_secs = int((end_time - start_time).total_seconds())
        except Exception:  # noqa: BLE001
            duration_secs = None

    record: dict[str, Any] = {
        "call_control_id": call_control_id,
        "row": state.get("row"),
        "sheet_id": state.get("sheet_id"),
        "hangup_cause": hangup_cause,
        "hangup_source": hangup_source,
        "duration_secs": duration_secs,
        "ended_at": ended_at.isoformat(),
        "to": to,
    }
    line = json.dumps(record, separators=(",", ":"))
    path = _resolve_path(spool_path)
    try:
        await asyncio.to_thread(_atomic_append, path, line)
        logger.info(
            "Hangup spooled: call=%s row=%s cause=%s -> %s",
            call_control_id,
            record["row"],
            hangup_cause,
            path,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to append to hangup spool %s", path)
