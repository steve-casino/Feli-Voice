# Felicetti Law Firm — Voice Agent

A FastAPI service that answers inbound calls to the Felicetti Law Firm and holds a real-time spoken conversation with the caller. The audio pipeline is:

```
PSTN caller
  └─► Telnyx (purchased number + Voice API Application)
        └─► HTTPS webhook  →  POST /telnyx/webhook   (we answer the call)
        └─► Media WebSocket →  WS   /telnyx/media    (bidirectional audio)
              ├─► Deepgram (streaming STT, μ-law 8kHz)
              ├─► Anthropic Claude (streaming reply)
              └─► ElevenLabs (streaming TTS, μ-law 8kHz)
```

Remote: <https://github.com/steve-casino/Feli-Voice.git>

## Project layout

```
feli_voice_agent/
├── .env.example         # Variable template (safe to commit)
├── .env                 # Real keys (gitignored)
├── .gitignore
├── README.md
├── requirements.txt
├── main.py              # FastAPI app, router registration, /health
└── app/
    ├── config.py        # Settings loaded from .env
    ├── handlers/
    │   ├── telnyx_webhook.py   # POST /telnyx/webhook
    │   └── telnyx_media.py     # WS   /telnyx/media + orchestrator
    ├── services/
    │   ├── telnyx_service.py     # Call control + signature verification
    │   ├── deepgram_service.py   # Streaming STT client
    │   ├── anthropic_service.py  # Streaming LLM client
    │   └── elevenlabs_service.py # Streaming TTS client
    ├── prompts/
    │   └── persona.py            # System prompt for the receptionist
    ├── models/
    │   └── telnyx.py             # Pydantic models for Telnyx payloads
    └── tools/                    # (empty for now; tools land in next milestone)
```

## Setup

1. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate            # Windows PowerShell
   source .venv/bin/activate         # macOS / Linux
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Populate `.env` with real credentials. `.env.example` shows the template. Required for this milestone: `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `TELNYX_API_KEY`, `APP_BASE_URL`. Google variables can stay blank until we add tool calls.

## Running the app

For set-and-forget operation (FastAPI app + Cloudflare tunnel together, with crash auto-restart), one command:

```bash
python run.py
```

`run.py` is a supervisor that spawns `uvicorn main:app` and `cloudflared tunnel run feli-voice`, restarts either if it crashes (capped exponential backoff), prefixes log lines with `[uvicorn]` / `[tunnel]`, and shuts both down cleanly on Ctrl+C.

For dev with hot reload, run uvicorn directly instead:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Smoke-check: visit <http://localhost:8000/health>. You should see:

```json
{"status": "ok", "app_base_url": "https://api.felivoice.com", "media_ws_url": "wss://api.felivoice.com/telnyx/media"}
```

## Exposing it publicly via Cloudflare Tunnel

In a second terminal, run a **named** Cloudflare Tunnel that maps `api.felivoice.com` → `localhost:8000`. One-time setup:

```bash
cloudflared tunnel login                       # browser auth, pick felivoice.com
cloudflared tunnel create feli-voice
```

Then in the Cloudflare dashboard: **Zero Trust → Networks → Tunnels →** `feli-voice` **→ Public Hostnames → Add a public hostname**:

- Subdomain: `api`
- Domain: `felivoice.com`
- Service: HTTP → `http://localhost:8000`

After that, every time you develop:

```bash
cloudflared tunnel run feli-voice
```

The tunnel terminates TLS at Cloudflare and forwards both HTTPS and WebSocket traffic to your local server.

## Telnyx configuration checklist

In the Telnyx Mission Control Portal:

1. **Voice → Programmable Voice → Applications** → your Voice API Application:
   - **Webhook URL:** `https://api.felivoice.com/telnyx/webhook`
   - **Webhook API Version:** API v2
2. **Numbers → My Numbers** → your purchased number → **Voice** tab → set **Connection or App** to the Voice API Application above.

That's it for inbound calls — no SIP Connection or Outbound Voice Profile needed for this milestone.

## Placing a test call

1. Start uvicorn.
2. Start `cloudflared tunnel run feli-voice`.
3. Call your Telnyx phone number from a real phone.

You should hear: *"Thank you for calling Felicetti Law Firm. This is the firm's assistant. How can I help you today?"* — and then a real two-way conversation.

## What the logs mean

While a call is live, the FastAPI process logs interleaved lines like:

```
INFO  app.handlers.telnyx_webhook | Telnyx event call.initiated call=v3:... from=+1... to=+14047774002
INFO  app.handlers.telnyx_webhook | Answering call ... with stream -> wss://api.felivoice.com/telnyx/media
INFO  app.handlers.telnyx_media   | Telnyx media WS accepted
INFO  app.handlers.telnyx_media   | Caller: hi I'd like to schedule a consultation
INFO  app.handlers.telnyx_media   | Agent: Of course. May I have your name first?
```

If you don't see the `Caller:` line, audio isn't reaching Deepgram (check tunnel, check that media streaming was actually started — Telnyx debug logs help). If `Caller:` shows but `Agent:` never does, the LLM call is failing — check the stack trace.

## Environment variables

See `.env.example`. The key one for telephony is `APP_BASE_URL` — it tells the app what public URL Telnyx is dialing, which we use to construct the media WebSocket URL Telnyx connects back on.

## Outbound calling + OpenClaw dialer agent

The app can place outbound calls (`POST /calls/outbound`) — Telnyx dials the
number and the same Deepgram→Claude→ElevenLabs pipeline handles the
conversation, using the outbound persona/greeting in `app/prompts/persona.py`.

"Who to call" is driven by an **OpenClaw agent** that reads a **Google Sheet**
call list, applies judgment (per-contact business hours, retry caps, dedupe),
and dials due contacts on demand. The agent never talks to Telnyx directly — it
runs `tools/dialer.py`, which is the only thing that touches the sheet and the
app.

### 1. The call-list sheet

Create a Google Sheet. Row 1 is headers (case-insensitive); each later row is a
contact. `phone` is the only required column:

| Column | Purpose |
| --- | --- |
| `name` | Who you're calling (used in logs only) |
| `phone` | E.164 number, e.g. `+15551234567` (**required**) |
| `timezone` | IANA tz for business-hours logic, e.g. `America/New_York` (default if blank) |
| `status` | `queued` / `calling` / `done` / `failed` / `do_not_call` |
| `attempts` | Auto-incremented by the dialer |
| `max_attempts` | Retry cap (default 3) |
| `last_called` | Auto-stamped (ISO) |
| `last_outcome` | Call control id or free-text result |
| `window_start` / `window_end` | Local call window, e.g. `09:00` / `17:00` |
| `greeting` | Optional per-contact opening line |
| `notes` | Context the agent can use on the call |

### 2. Service-account auth (one-time, manual)

The agent runs unattended, so it uses a **service account** (not interactive
OAuth):

1. In Google Cloud Console: create/select a project → **Enable the Google
   Sheets API**.
2. **IAM & Admin → Service Accounts → Create** → create a JSON key, download it.
3. Save the JSON somewhere readable (e.g. `~/.config/feli/sa.json`).
4. **Share the Sheet** with the service account's `client_email` (from the JSON)
   as **Editor**.
5. In `.env` set `GOOGLE_SHEETS_ID` (the long id in the sheet URL) and
   `GOOGLE_SERVICE_ACCOUNT_FILE` (path to the JSON).

### 3. The dialer CLI

```bash
.venv/bin/python tools/dialer.py queue            # call list as JSON (+ row #s, local time, hints)
.venv/bin/python tools/dialer.py call --row 5     # dial that row, mark status=calling, attempts++
.venv/bin/python tools/dialer.py mark --row 5 --status done --note "booked consult"
.venv/bin/python tools/dialer.py flush            # drain hangup spool into the sheet
```

`queue` adds computed hints per contact (`local_now`, `within_business_hours`,
`attempts_remaining`, `terminal`, `callable_hint`) so the agent can decide who's
due. They're hints — the agent makes the final call.

### 3a. Outcome feedback loop

When `call` dials, it tells the voice app the sheet row. The app round-trips
that row through Telnyx's `client_state`, so when Telnyx fires `call.hangup`
the webhook can attribute the outcome back to the right row without any
in-memory mapping. The webhook appends one JSON line per hangup to
`HANGUP_SPOOL_PATH` (default `~/Library/Application Support/felicetti-voice/hangups.jsonl`),
and `queue` / `call` drain that spool into the sheet (atomic rename → read →
batchUpdate → unlink) before doing anything else. Net effect: every run starts
by reconciling the previous run's outcomes.

`hangup_cause` is mapped to `last_outcome` (and `status`) like this:

| Telnyx `hangup_cause` | `status` | `last_outcome` |
| --- | --- | --- |
| `normal_clearing` | `done` | `answered (Ns)` |
| `no_answer` | `queued` | `no_answer (Ns)` |
| `user_busy` | `queued` | `busy (Ns)` |
| `call_rejected` | `queued` | `declined (Ns)` |
| `invalid_number_format` / `unallocated_number` | `do_not_call` | `invalid_number` / `unallocated_number` |
| anything else | `queued` | `<raw cause> (Ns)` |

Without Telnyx **Answering Machine Detection**, `normal_clearing` cannot
distinguish answered-by-human from voicemail-picked-up — both read as
"contact made, move on." Enable AMD on the dial if you need to retry only
no-answer / voicemail.

### 4. The OpenClaw agent (on-demand)

The dialer is driven by a **disabled** OpenClaw cron job named `feli-dialer`
(model pinned to `anthropic/claude-sonnet-4-6` — the default `gpt-5.4-mini`
agent does not expose the `exec` tool, so it can't run the wrapper). You trigger
it manually. `cron run` takes the job **id**, so grab it then run:

```bash
openclaw cron list                       # find the feli-dialer id
openclaw cron run <feli-dialer-id>       # dial everyone due, now
```

The agent reads the queue, picks who's due (respecting business hours, retry
caps, and dedupe), dials them via `tools/dialer.py`, and writes outcomes back to
the sheet. It does **not** run on a schedule — nothing dials unless you trigger
it. Read the result of a run with:

```bash
openclaw cron runs --id <feli-dialer-id>
```

To edit the calling policy, change `tools/dialer_agent_prompt.txt`, then push it
to the job: `openclaw cron edit <id> --message "$(cat tools/dialer_agent_prompt.txt)"`.

## What's next

After this milestone, the next pieces are:

- **Google OAuth** to enable calendar booking + email confirmations.
- **Tool calls** wired into Claude (`book_consultation`, `take_message`, `send_email_confirmation`).
- **Call transfer** to a real attorney when the caller asks.
- **Outbound Voice Profile** in Telnyx (required for transfers and callbacks).
