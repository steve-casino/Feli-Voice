"""Outbound-call dialer CLI — the bridge the OpenClaw agent drives.

The agent does the *judgment* (who to call, when, retry/dedupe). This script
gives it four primitives over the Google Sheet call list + the local voice app:

    python tools/dialer.py queue            # flush hangups + read the call list
    python tools/dialer.py call --row 5     # dial that contact, mark the row
    python tools/dialer.py mark --row 5 --status done --note "left voicemail"
    python tools/dialer.py flush            # only drain the hangup spool

Why a Sheet: non-technical staff edit the list; the agent reads/writes it.
Why service-account auth: the agent runs unattended under OpenClaw, so there's
no human present to complete an OAuth consent flow.

How the feedback loop works: when `call` dials, it tells the voice app the
sheet row. The app round-trips that row through Telnyx's `client_state`. On
hangup, the webhook appends a JSON line to a local spool. `queue` and `call`
both drain that spool into the sheet before they do anything else, so the
agent always sees the latest outcomes (answered / no_answer / busy / etc.).

Config (read from the repo .env or the environment):
    GOOGLE_SHEETS_ID            spreadsheet id (the long id in the sheet URL)
    GOOGLE_SERVICE_ACCOUNT_FILE path to the service-account JSON key
    FELI_APP_URL                voice app base url (default http://localhost:8000)
    HANGUP_SPOOL_PATH           where the voice app appends hangup events
                                (default ~/Library/Application Support/felicetti-voice/hangups.jsonl)

Expected sheet columns (row 1 = headers, case-insensitive). Missing optional
columns are simply ignored:
    name, phone, timezone, status, attempts, max_attempts,
    last_called, last_outcome, window_start, window_end, greeting, notes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_APP_URL = "http://localhost:8000"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_WINDOW = ("09:00", "20:00")  # local business hours if a row omits them

# Statuses that mean "do not dial again", used by the agent to dedupe.
TERMINAL_STATUSES = {"done", "do_not_call", "completed", "dnc"}

DEFAULT_SPOOL_PATH = "~/Library/Application Support/felicetti-voice/hangups.jsonl"

# Telnyx hangup_cause -> (new_status, normalized_outcome).
# - normal_clearing means the call ended cleanly. Without Answering Machine
#   Detection we can't tell answered-by-human from voicemail-picked-up; both
#   read as "we made contact, move on."
# - everything retry-eligible resets status to "queued" so the agent's queue
#   logic picks it up next run, bounded by max_attempts.
# Unknown causes are conservative: leave row queued so a human can decide.
HANGUP_CAUSE_OUTCOME: dict[str, tuple[str, str]] = {
    "normal_clearing": ("done", "answered"),
    "no_answer": ("queued", "no_answer"),
    "user_busy": ("queued", "busy"),
    "call_rejected": ("queued", "declined"),
    "originator_cancel": ("queued", "originator_cancel"),
    "destination_out_of_order": ("queued", "destination_out_of_order"),
    "invalid_number_format": ("do_not_call", "invalid_number"),
    "unallocated_number": ("do_not_call", "unallocated_number"),
    "recovery_on_timer_expire": ("queued", "timeout"),
    "normal_temporary_failure": ("queued", "temporary_failure"),
}


def _fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(json.dumps({"ok": False, "error": msg}), file=sys.stderr)
    sys.exit(1)


def _sheets_client():
    sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not sa_file:
        _fail(
            "GOOGLE_SERVICE_ACCOUNT_FILE is not set. Point it at the "
            "service-account JSON key and share the sheet with that account."
        )
    if not Path(sa_file).expanduser().is_file():
        _fail(f"Service-account file not found: {sa_file}")
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        _fail(f"Google API libraries missing: {exc}")
    creds = service_account.Credentials.from_service_account_file(
        str(Path(sa_file).expanduser()), scopes=[SHEETS_SCOPE]
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _sheet_id() -> str:
    sid = os.getenv("GOOGLE_SHEETS_ID")
    if not sid:
        _fail("GOOGLE_SHEETS_ID is not set (the long id from the sheet URL).")
    return sid


def _tab_title(svc, spreadsheet_id: str) -> str:
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = meta.get("sheets") or []
    if not sheets:
        _fail("Spreadsheet has no tabs.")
    return sheets[0]["properties"]["title"]


def _read_grid(svc, spreadsheet_id: str, title: str) -> list[list[str]]:
    resp = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{title}'")
        .execute()
    )
    return resp.get("values", [])


def _headers(grid: list[list[str]]) -> dict[str, int]:
    if not grid:
        _fail("Sheet is empty — add a header row first.")
    return {h.strip().lower(): i for i, h in enumerate(grid[0]) if h.strip()}


def _cell(row: list[str], cols: dict[str, int], name: str, default: str = "") -> str:
    idx = cols.get(name)
    if idx is None or idx >= len(row):
        return default
    return (row[idx] or "").strip()


def _col_letter(idx: int) -> str:
    letters = ""
    n = idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _local_now(tz: str) -> datetime | None:
    try:
        return datetime.now(ZoneInfo(tz))
    except Exception:
        return None


def _within_window(now: datetime | None, start: str, end: str) -> bool | None:
    if now is None:
        return None
    s = start or DEFAULT_WINDOW[0]
    e = end or DEFAULT_WINDOW[1]
    try:
        sh, sm = map(int, s.split(":"))
        eh, em = map(int, e.split(":"))
    except ValueError:
        return None
    minutes = now.hour * 60 + now.minute
    return sh * 60 + sm <= minutes <= eh * 60 + em


def _load_rows(svc, spreadsheet_id: str, title: str) -> tuple[dict[str, int], list[dict[str, Any]]]:
    grid = _read_grid(svc, spreadsheet_id, title)
    cols = _headers(grid)
    if "phone" not in cols:
        _fail("Sheet needs a 'phone' column.")
    rows: list[dict[str, Any]] = []
    for i, raw in enumerate(grid[1:], start=2):  # row 1 is the header; sheet is 1-based
        phone = _cell(raw, cols, "phone")
        if not phone:
            continue
        tz = _cell(raw, cols, "timezone") or "America/New_York"
        now = _local_now(tz)
        status = _cell(raw, cols, "status").lower() or "queued"
        try:
            attempts = int(_cell(raw, cols, "attempts") or "0")
        except ValueError:
            attempts = 0
        try:
            max_attempts = int(_cell(raw, cols, "max_attempts") or DEFAULT_MAX_ATTEMPTS)
        except ValueError:
            max_attempts = DEFAULT_MAX_ATTEMPTS
        win_start = _cell(raw, cols, "window_start")
        win_end = _cell(raw, cols, "window_end")
        rows.append(
            {
                "row": i,
                "name": _cell(raw, cols, "name"),
                "phone": phone,
                "timezone": tz,
                "status": status,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "last_called": _cell(raw, cols, "last_called"),
                "last_outcome": _cell(raw, cols, "last_outcome"),
                "greeting": _cell(raw, cols, "greeting"),
                "notes": _cell(raw, cols, "notes"),
                "window_start": win_start or DEFAULT_WINDOW[0],
                "window_end": win_end or DEFAULT_WINDOW[1],
                "local_now": now.strftime("%Y-%m-%d %H:%M") if now else None,
                "within_business_hours": _within_window(now, win_start, win_end),
                "terminal": status in TERMINAL_STATUSES,
                "attempts_remaining": max(max_attempts - attempts, 0),
                # The agent should treat this as a hint, not a hard gate.
                "callable_hint": (
                    status not in TERMINAL_STATUSES
                    and attempts < max_attempts
                    and _within_window(now, win_start, win_end) is True
                ),
            }
        )
    return cols, rows


def _write_row(
    svc, spreadsheet_id: str, title: str, cols: dict[str, int], row: int, values: dict[str, str]
) -> None:
    data = []
    for name, value in values.items():
        idx = cols.get(name)
        if idx is None:
            continue  # column doesn't exist in the sheet; skip silently
        a1 = f"'{title}'!{_col_letter(idx)}{row}"
        data.append({"range": a1, "values": [[value]]})
    if not data:
        return
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


# ----------------------------------------------------------------------------
# Hangup feedback spool — drains the voice app's hangups.jsonl into the sheet.
# ----------------------------------------------------------------------------


def _spool_path() -> Path:
    return Path(os.path.expanduser(os.getenv("HANGUP_SPOOL_PATH") or DEFAULT_SPOOL_PATH))


def _claim_spool(path: Path) -> Path | None:
    """Atomically take ownership of the active spool file.

    Returns the processing path if there is anything to drain (either a
    leftover .processing from a previous crash, or a freshly renamed file),
    or None if there's nothing pending.

    POSIX rename(2) is atomic; any webhook write already in flight finishes
    against the same inode (which now lives at .processing), and subsequent
    webhook calls open a fresh hangups.jsonl. Lines cannot be lost or
    duplicated across the swap.
    """
    processing = path.with_suffix(path.suffix + ".processing")
    if processing.exists():
        return processing  # left behind by a prior crash; finish it
    if not path.exists():
        return None
    try:
        path.rename(processing)
    except FileNotFoundError:
        return None
    return processing


def _parse_spool(processing: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in processing.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError:
            # Garbage line — record and keep going so one bad write doesn't
            # block the whole flush.
            records.append({"_unparseable": raw})
    return records


def _outcome_for(cause: str | None, duration: int | None) -> tuple[str, str]:
    """Map a Telnyx hangup_cause to (new_status, outcome_label)."""
    if not cause:
        return ("queued", "unknown")
    mapping = HANGUP_CAUSE_OUTCOME.get(cause)
    if mapping is not None:
        new_status, label = mapping
    else:
        new_status, label = ("queued", cause)
    # Surface duration in the human-visible outcome so missed-call vs short
    # answered call is distinguishable at a glance, even without AMD.
    if duration is not None:
        label = f"{label} ({duration}s)"
    return new_status, label


def _flush_hangups(svc, spreadsheet_id: str, title: str, cols: dict[str, int]) -> dict[str, Any]:
    """Drain the spool into the sheet. Idempotent — safe to call from any
    command. Returns a small summary the caller can include in JSON output."""
    spool = _spool_path()
    processing = _claim_spool(spool)
    if processing is None:
        return {"flushed": 0, "skipped": 0, "errors": []}

    records = _parse_spool(processing)
    flushed = 0
    skipped = 0
    errors: list[str] = []

    # Only the latest hangup per row wins if the spool has multiple events
    # for the same row (e.g. retried webhooks). Spool order is append-order,
    # so iterating forwards naturally lets the last one overwrite earlier ones.
    by_row: dict[int, dict[str, Any]] = {}
    for rec in records:
        if "_unparseable" in rec:
            errors.append(f"unparseable: {rec['_unparseable'][:120]}")
            continue
        row = rec.get("row")
        if not isinstance(row, int):
            skipped += 1
            continue
        # If the sheet_id round-trip is present, make sure we don't blindly
        # write a hangup from a different spreadsheet onto this one.
        rec_sid = rec.get("sheet_id")
        if rec_sid and rec_sid != spreadsheet_id:
            skipped += 1
            errors.append(f"row {row} ignored: sheet_id mismatch ({rec_sid})")
            continue
        by_row[row] = rec

    for row, rec in by_row.items():
        cause = rec.get("hangup_cause")
        duration = rec.get("duration_secs")
        new_status, outcome = _outcome_for(cause, duration if isinstance(duration, int) else None)
        try:
            _write_row(
                svc,
                spreadsheet_id,
                title,
                cols,
                row,
                {"status": new_status, "last_outcome": outcome},
            )
            flushed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"row {row}: {exc}")

    # Only delete the processing file once writes succeeded. If any errored,
    # leave it in place so the next run retries; sheet writes are idempotent.
    if not errors:
        try:
            processing.unlink()
        except FileNotFoundError:
            pass
    else:
        # Partial success: rewrite the processing file with only the rows that
        # failed, so we don't keep re-flushing the successful ones.
        failed_rows = {
            int(e.split()[1].rstrip(":")) for e in errors if e.startswith("row ")
        }
        if failed_rows:
            keep = [r for r in records if r.get("row") in failed_rows]
            processing.write_text(
                "\n".join(json.dumps(r, separators=(",", ":")) for r in keep) + "\n",
                encoding="utf-8",
            )
        else:
            # Only header-level errors (e.g. unparseable); drop the file so
            # we don't loop forever.
            try:
                processing.unlink()
            except FileNotFoundError:
                pass

    return {"flushed": flushed, "skipped": skipped, "errors": errors}


def cmd_queue(_args: argparse.Namespace) -> None:
    svc = _sheets_client()
    sid = _sheet_id()
    title = _tab_title(svc, sid)
    cols, _ = _load_rows(svc, sid, title)
    flush_summary = _flush_hangups(svc, sid, title, cols)
    # Re-read after flush so the returned queue reflects the just-written outcomes.
    _cols, rows = _load_rows(svc, sid, title)
    print(json.dumps(
        {"ok": True, "tab": title, "count": len(rows), "flush": flush_summary, "contacts": rows},
        indent=2,
    ))


def cmd_flush(_args: argparse.Namespace) -> None:
    svc = _sheets_client()
    sid = _sheet_id()
    title = _tab_title(svc, sid)
    cols, _ = _load_rows(svc, sid, title)
    summary = _flush_hangups(svc, sid, title, cols)
    print(json.dumps({"ok": True, **summary}, indent=2))


def _find_row(rows: list[dict[str, Any]], row_num: int) -> dict[str, Any]:
    for r in rows:
        if r["row"] == row_num:
            return r
    _fail(f"No contact at row {row_num}.")


def cmd_call(args: argparse.Namespace) -> None:
    svc = _sheets_client()
    sid = _sheet_id()
    title = _tab_title(svc, sid)
    cols, _ = _load_rows(svc, sid, title)
    # Drain any pending hangups first so the gating checks below (terminal,
    # attempts_remaining) see the latest outcomes from prior calls.
    flush_summary = _flush_hangups(svc, sid, title, cols)
    _cols, rows = _load_rows(svc, sid, title)
    contact = _find_row(rows, args.row)

    if contact["terminal"] and not args.force:
        _fail(f"Row {args.row} status is '{contact['status']}' (terminal). Use --force to override.")
    if contact["attempts_remaining"] <= 0 and not args.force:
        _fail(f"Row {args.row} has no attempts remaining. Use --force to override.")

    greeting = args.greeting or contact.get("greeting") or None
    app_url = (os.getenv("FELI_APP_URL") or DEFAULT_APP_URL).rstrip("/")
    body: dict[str, Any] = {
        "to": contact["phone"],
        # The voice app round-trips these through Telnyx client_state so the
        # hangup webhook can attribute the outcome back to this row without
        # any local in-memory mapping.
        "row": args.row,
        "sheet_id": sid,
    }
    if greeting:
        body["greeting"] = greeting

    try:
        resp = httpx.post(f"{app_url}/calls/outbound", json=body, timeout=15.0)
    except httpx.HTTPError as exc:
        _fail(f"Could not reach voice app at {app_url}: {exc}")
    if resp.status_code >= 400:
        _fail(f"Voice app returned {resp.status_code}: {resp.text}")

    data = resp.json()
    ccid = data.get("call_control_id")
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_row(
        svc, sid, title, cols, args.row,
        {
            "status": "calling",
            "attempts": str(contact["attempts"] + 1),
            "last_called": now_iso,
            "last_outcome": ccid or "dialed",
        },
    )
    print(json.dumps({
        "ok": True, "row": args.row, "to": contact["phone"],
        "call_control_id": ccid, "attempt": contact["attempts"] + 1,
        "flush": flush_summary,
    }, indent=2))


def cmd_mark(args: argparse.Namespace) -> None:
    svc = _sheets_client()
    sid = _sheet_id()
    title = _tab_title(svc, sid)
    cols, rows = _load_rows(svc, sid, title)
    _find_row(rows, args.row)  # validate the row exists
    values: dict[str, str] = {}
    if args.status:
        values["status"] = args.status
    if args.note is not None:
        values["last_outcome"] = args.note
    if not values:
        _fail("Nothing to update — pass --status and/or --note.")
    _write_row(svc, sid, title, cols, args.row, values)
    print(json.dumps({"ok": True, "row": args.row, "updated": values}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Felicetti outbound dialer")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("queue", help="Print the call list as JSON").set_defaults(func=cmd_queue)

    p_call = sub.add_parser("call", help="Dial a contact by sheet row and mark the row")
    p_call.add_argument("--row", type=int, required=True, help="Sheet row number (from `queue`)")
    p_call.add_argument("--greeting", default=None, help="Override opening line")
    p_call.add_argument("--force", action="store_true", help="Dial even if terminal/no attempts left")
    p_call.set_defaults(func=cmd_call)

    p_mark = sub.add_parser("mark", help="Write back status/outcome for a row")
    p_mark.add_argument("--row", type=int, required=True)
    p_mark.add_argument("--status", default=None, help="e.g. done, failed, do_not_call, queued")
    p_mark.add_argument("--note", default=None, help="Free-text outcome")
    p_mark.set_defaults(func=cmd_mark)

    sub.add_parser(
        "flush",
        help="Drain the hangup spool into the sheet (rarely needed; queue/call run it implicitly)",
    ).set_defaults(func=cmd_flush)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
