"""Internal callback dashboard.

Renders the intake spool as a mobile-friendly table at GET /intakes, gated
by HTTP Basic auth. Staff can expand transcripts, mark a row as called
back, or skip it. All actions append to intake_actions.jsonl — the source
intake spool stays untouched.

Threat model: this is on the public internet behind a single shared
password. The route applies a per-IP rate limiter to brute-force attempts
on the basic-auth path so even a weak password isn't trivially crackable.
That's defense-in-depth — rotate the password for real protection.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings
from app.services import intake_actions

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/intakes", tags=["dashboard"])
_basic = HTTPBasic(auto_error=False)


# ---------------------------------------------------------------------------
# Auth + rate limiting
# ---------------------------------------------------------------------------

# Per-IP sliding window: (timestamp of attempts).
_AUTH_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_AUTH_WINDOW_SECS = 60.0
_AUTH_MAX_ATTEMPTS = 5
_AUTH_LOCKOUT_SECS = 300.0
_AUTH_LOCKOUT_UNTIL: dict[str, float] = {}


def _client_ip(request: Request) -> str:
    # Cloudflare sets CF-Connecting-IP with the real client IP. Fall back to
    # the immediate peer if we're somehow not behind Cloudflare.
    return (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    locked_until = _AUTH_LOCKOUT_UNTIL.get(ip)
    if locked_until and now < locked_until:
        retry = int(locked_until - now)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many auth attempts; retry in {retry}s.",
            headers={"Retry-After": str(retry)},
        )
    # Drop attempts outside the window.
    attempts = [t for t in _AUTH_ATTEMPTS[ip] if now - t < _AUTH_WINDOW_SECS]
    _AUTH_ATTEMPTS[ip] = attempts


def _record_failed_auth(ip: str) -> None:
    now = time.monotonic()
    _AUTH_ATTEMPTS[ip].append(now)
    if len(_AUTH_ATTEMPTS[ip]) >= _AUTH_MAX_ATTEMPTS:
        _AUTH_LOCKOUT_UNTIL[ip] = now + _AUTH_LOCKOUT_SECS
        logger.warning(
            "Dashboard auth: locking out ip=%s for %ds after %d failed attempts",
            ip,
            int(_AUTH_LOCKOUT_SECS),
            len(_AUTH_ATTEMPTS[ip]),
        )


def _clear_attempts(ip: str) -> None:
    _AUTH_ATTEMPTS.pop(ip, None)
    _AUTH_LOCKOUT_UNTIL.pop(ip, None)


def require_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> str:
    """Validate basic-auth, return the authenticated username.

    If the dashboard password isn't configured, fail closed with a 503 so
    we never accidentally serve intake data unauthenticated.
    """
    if not settings.dashboard_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Dashboard is disabled — set DASHBOARD_PASSWORD in .env "
                "and restart."
            ),
        )

    ip = _client_ip(request)
    _check_rate_limit(ip)

    if credentials is None:
        # Prompt the browser for credentials.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="Felicetti Intake"'},
        )

    # Timing-safe comparison so a network observer can't deduce the password
    # length from response timing.
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.dashboard_user.encode("utf-8"),
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.dashboard_password.encode("utf-8"),
    )
    if not (user_ok and pass_ok):
        _record_failed_auth(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Felicetti Intake"'},
        )

    _clear_attempts(ip)
    return credentials.username


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _relative_time(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    return dt.strftime("%b %d")


def _absolute_time(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def _matter_label(summary: str | None) -> str:
    """Pull the 'Matter:' line out of the model's English summary."""
    if not summary:
        return "—"
    for line in summary.splitlines():
        if line.lower().startswith("matter:"):
            return line.split(":", 1)[1].strip()
    return "—"


def _name_label(summary: str | None, fallback: str | None = None) -> str:
    if not summary:
        return fallback or "Unknown caller"
    for line in summary.splitlines():
        if line.lower().startswith("name:"):
            return line.split(":", 1)[1].strip() or (fallback or "Unknown caller")
    return fallback or "Unknown caller"


def _urgency_label(summary: str | None) -> str:
    if not summary:
        return "routine"
    for line in summary.splitlines():
        if line.lower().startswith("urgency:"):
            val = line.split(":", 1)[1].strip().lower()
            return "urgent" if "urgent" in val else "routine"
    return "routine"


def _join_records(
    intakes: list[dict[str, Any]],
    actions: dict[str, dict[str, Any]],
    *,
    filter_status: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for intake in intakes:
        ccid = intake.get("call_control_id") or ""
        action = actions.get(ccid)
        st = intake_actions.status_for(action)
        if filter_status == "open" and st != "open":
            continue
        if filter_status == "urgent" and _urgency_label(intake.get("summary")) != "urgent":
            continue
        ended = _parse_iso(intake.get("ended_at"))
        rows.append(
            {
                "call_control_id": ccid,
                "name": _name_label(intake.get("summary"), intake.get("from")),
                "phone": intake.get("from") or "—",
                "language": (intake.get("language") or "?").upper(),
                "matter": _matter_label(intake.get("summary")),
                "urgency": _urgency_label(intake.get("summary")),
                "summary": intake.get("summary") or "",
                "transcript": intake.get("transcript") or [],
                "ended": ended,
                "rel_time": _relative_time(ended),
                "abs_time": _absolute_time(ended),
                "status": st,
                "action": action,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# HTML rendering (inline so we add no template-engine dependency)
# ---------------------------------------------------------------------------


_PAGE_HEAD = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Felicetti — Callback Queue</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/htmx.org@1.9.10"></script>
  <style>
    :root { color-scheme: light; }
    body { background: #f6f7f9; }
  </style>
</head>
<body class="text-slate-800">
"""

_PAGE_FOOT = "</body></html>"


def _status_pill(status_value: str) -> str:
    color = {
        "open": "bg-amber-100 text-amber-900",
        "called_back": "bg-emerald-100 text-emerald-900",
        "skipped": "bg-slate-200 text-slate-700",
    }.get(status_value, "bg-slate-200 text-slate-700")
    label = {
        "open": "Open",
        "called_back": "Called back",
        "skipped": "Skipped",
    }.get(status_value, status_value.title())
    return (
        f'<span class="inline-flex items-center px-2 py-0.5 rounded-full '
        f'text-xs font-medium {color}">{escape(label)}</span>'
    )


def _urgency_pill(urgency: str) -> str:
    if urgency == "urgent":
        return (
            '<span class="inline-flex items-center px-2 py-0.5 rounded-full '
            'text-xs font-semibold bg-rose-100 text-rose-800 ml-1">Urgent</span>'
        )
    return ""


def _language_pill(language: str) -> str:
    color = "bg-sky-100 text-sky-900" if language == "EN" else "bg-violet-100 text-violet-900"
    return (
        f'<span class="inline-flex items-center px-2 py-0.5 rounded-full '
        f'text-xs font-semibold {color}">{escape(language)}</span>'
    )


def _filter_chip(label: str, value: str, current: str) -> str:
    base = "px-3 py-1 rounded-full text-sm font-medium transition"
    if value == current:
        cls = f"{base} bg-slate-900 text-white"
    else:
        cls = f"{base} bg-white text-slate-700 hover:bg-slate-100 border border-slate-200"
    return f'<a class="{cls}" href="?filter={value}">{escape(label)}</a>'


def _render_row(row: dict[str, Any]) -> str:
    ccid = row["call_control_id"]
    phone = row["phone"] or ""
    tel_link = phone if phone.startswith("+") else ""
    name = row["name"]
    matter = row["matter"]
    summary_lines = [escape(line) for line in (row["summary"] or "").splitlines() if line.strip()]
    summary_block = "<br>".join(summary_lines) if summary_lines else "<em>No summary.</em>"
    return f"""
    <article id="row-{escape(ccid)}" class="bg-white rounded-2xl shadow-sm border border-slate-200 p-4 mb-3">
      <div class="flex items-start justify-between gap-3 flex-wrap">
        <div class="min-w-0">
          <div class="flex items-center gap-2 flex-wrap">
            <h2 class="text-lg font-semibold truncate">{escape(name)}</h2>
            {_language_pill(row["language"])}
            {_urgency_pill(row["urgency"])}
            {_status_pill(row["status"])}
          </div>
          <div class="mt-1 text-sm text-slate-500">
            <span title="{escape(row["abs_time"])}">{escape(row["rel_time"])}</span>
            <span class="mx-1">•</span>
            <span>{escape(matter)}</span>
          </div>
        </div>
        <div class="flex items-center gap-2">
          {'<a class="px-3 py-1.5 rounded-lg bg-sky-600 text-white text-sm font-medium" href="tel:' + escape(tel_link) + '">Call ' + escape(phone) + '</a>' if tel_link else '<span class="text-sm text-slate-400">No number</span>'}
        </div>
      </div>
      <div class="mt-3 text-sm text-slate-700 leading-snug">{summary_block}</div>
      <div class="mt-3 flex items-center gap-2 flex-wrap">
        <button class="px-3 py-1.5 rounded-lg border border-slate-300 text-sm hover:bg-slate-100"
                hx-get="/intakes/{escape(ccid)}/transcript"
                hx-target="#tx-{escape(ccid)}"
                hx-swap="innerHTML"
                onclick="this.disabled=true;this.textContent='Loading…';">
          View transcript
        </button>
        <form hx-post="/intakes/{escape(ccid)}/action"
              hx-target="#row-{escape(ccid)}"
              hx-swap="outerHTML"
              class="contents">
          <input type="hidden" name="action" value="called_back">
          <button class="px-3 py-1.5 rounded-lg bg-emerald-600 text-white text-sm hover:bg-emerald-700">
            Mark called back
          </button>
        </form>
        <form hx-post="/intakes/{escape(ccid)}/action"
              hx-target="#row-{escape(ccid)}"
              hx-swap="outerHTML"
              class="contents">
          <input type="hidden" name="action" value="skipped">
          <button class="px-3 py-1.5 rounded-lg bg-slate-100 text-slate-700 text-sm hover:bg-slate-200">
            Skip
          </button>
        </form>
        {('<form hx-post="/intakes/' + escape(ccid) + '/action" hx-target="#row-' + escape(ccid) + '" hx-swap="outerHTML" class="contents"><input type="hidden" name="action" value="reopened"><button class="px-3 py-1.5 rounded-lg border border-slate-300 text-sm hover:bg-slate-100">Reopen</button></form>') if row["status"] != "open" else ""}
      </div>
      <div id="tx-{escape(ccid)}" class="mt-3 text-sm"></div>
    </article>
    """


def _render_page(rows: list[dict[str, Any]], current_filter: str) -> str:
    chips = (
        _filter_chip("Open", "open", current_filter)
        + _filter_chip("All", "all", current_filter)
        + _filter_chip("Urgent", "urgent", current_filter)
    )
    if not rows:
        body = (
            '<div class="text-center text-slate-500 py-16">'
            '<div class="text-2xl mb-2">No callbacks in this view.</div>'
            '<div class="text-sm">When a caller leaves a message overnight it will appear here.</div>'
            "</div>"
        )
    else:
        body = "".join(_render_row(r) for r in rows)
    return f"""{_PAGE_HEAD}
    <div class="max-w-3xl mx-auto px-4 py-6">
      <header class="flex items-center justify-between mb-4 flex-wrap gap-3">
        <h1 class="text-2xl font-semibold tracking-tight">Callback Queue</h1>
        <div class="text-xs text-slate-500">Auto-refreshes every 60 s</div>
      </header>
      <div class="flex items-center gap-2 flex-wrap mb-4">{chips}</div>
      <div hx-get="?filter={escape(current_filter)}&partial=1"
           hx-trigger="every 60s"
           hx-target="#queue"
           hx-swap="innerHTML"
           hx-select="#queue > *">
        <div id="queue">{body}</div>
      </div>
    </div>
    {_PAGE_FOOT}"""


def _render_partial(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (
            '<div id="queue"><div class="text-center text-slate-500 py-16">'
            "No callbacks in this view.</div></div>"
        )
    return '<div id="queue">' + "".join(_render_row(r) for r in rows) + "</div>"


def _render_transcript(transcript: list[dict[str, Any]]) -> str:
    if not transcript:
        return '<div class="text-slate-500">No transcript captured.</div>'
    out: list[str] = ['<div class="rounded-xl bg-slate-50 border border-slate-200 p-3 max-h-72 overflow-y-auto">']
    for turn in transcript:
        role = (turn.get("role") or "").lower()
        content = escape(turn.get("content") or "")
        if role == "assistant":
            out.append(
                f'<div class="mb-2"><span class="font-semibold text-sky-700">Agent:</span> '
                f'<span class="text-slate-800">{content}</span></div>'
            )
        elif role == "user":
            out.append(
                f'<div class="mb-2"><span class="font-semibold text-emerald-700">Caller:</span> '
                f'<span class="text-slate-800">{content}</span></div>'
            )
        else:
            out.append(f'<div class="mb-2 text-slate-700">{content}</div>')
    out.append("</div>")
    return "".join(out)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def list_intakes(
    request: Request,
    filter: str = "open",
    partial: int = 0,
    _user: str = Depends(require_auth),
) -> HTMLResponse:
    """Render the callback queue. `filter`: open|all|urgent."""
    intakes = await asyncio.to_thread(intake_actions.load_intakes, settings.intake_spool_path)
    actions = await asyncio.to_thread(intake_actions.load_actions, settings.intake_actions_path)
    rows = _join_records(intakes, actions, filter_status=filter)
    if partial:
        return HTMLResponse(_render_partial(rows))
    return HTMLResponse(_render_page(rows, filter))


@router.get("/{call_control_id}/transcript", response_class=HTMLResponse)
async def get_transcript(
    call_control_id: str,
    _user: str = Depends(require_auth),
) -> HTMLResponse:
    intakes = await asyncio.to_thread(intake_actions.load_intakes, settings.intake_spool_path)
    intake = next((r for r in intakes if r.get("call_control_id") == call_control_id), None)
    if intake is None:
        return HTMLResponse(
            '<div class="text-slate-500">Intake not found.</div>',
            status_code=404,
        )
    return HTMLResponse(_render_transcript(intake.get("transcript") or []))


@router.post("/{call_control_id}/action", response_class=HTMLResponse)
async def post_action(
    call_control_id: str,
    action: str = Form(...),
    user: str = Depends(require_auth),
) -> HTMLResponse:
    if action not in intake_actions.VALID_ACTIONS:
        raise HTTPException(status_code=400, detail="Unknown action")
    await intake_actions.append_action(
        spool_path=settings.intake_actions_path,
        call_control_id=call_control_id,
        action=action,
        by=user,
    )
    # Re-render this single row with the new status so the dashboard updates
    # in place via htmx's hx-swap="outerHTML".
    intakes = await asyncio.to_thread(intake_actions.load_intakes, settings.intake_spool_path)
    actions = await asyncio.to_thread(intake_actions.load_actions, settings.intake_actions_path)
    intake = next((r for r in intakes if r.get("call_control_id") == call_control_id), None)
    if intake is None:
        return HTMLResponse("", status_code=204)
    row = _join_records([intake], actions, filter_status="all")
    if not row:
        return HTMLResponse("", status_code=204)
    return HTMLResponse(_render_row(row[0]))
