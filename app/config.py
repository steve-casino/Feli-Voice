"""Centralized configuration loaded from environment variables.

All env-var access in the app should go through `settings`. That way we have
exactly one place that validates which keys are required vs. optional, and
the rest of the code can rely on typed attributes rather than reaching into
`os.environ` ad hoc.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load .env once at import time. Safe to call multiple times.
load_dotenv()


def _env(key: str, default: str | None = None) -> str | None:
    """Read an env var, treating empty strings as missing."""
    value = os.getenv(key, default)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _require(key: str) -> str:
    value = _env(key)
    if value is None:
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Add it to your .env file (see .env.example for the template)."
        )
    return value


@dataclass(frozen=True, kw_only=True)
class Settings:
    """Application settings, populated from environment variables.

    Use `Settings.load()` to construct. Accessing required-but-missing keys
    raises at load time, not at first use, so misconfiguration shows up
    immediately at app startup.
    """

    # --- Anthropic ---
    anthropic_api_key: str
    # Sonnet 4.6 follows soft constraints (one-question-per-turn, language
    # mirroring, no-legal-advice) more reliably than Haiku in our testing.
    # First-token latency is ~300ms higher than Haiku but acceptable for voice.
    anthropic_model: str = "claude-sonnet-4-6"

    # --- Deepgram ---
    deepgram_api_key: str
    # Multilingual (en+es) code-switching. nova-3 is Deepgram's newest model;
    # its multilingual variant supports mulaw/8000 and is markedly more
    # accurate than nova-2 on noisy phone audio. Override with DEEPGRAM_MODEL
    # (e.g. nova-2) to fall back — the STT client adapts keyterm vs keywords
    # biasing to whichever model is set.
    deepgram_model: str = "nova-3"
    deepgram_language: str = "multi"

    # --- ElevenLabs ---
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    # `eleven_multilingual_v2` is the highest-quality model that supports
    # WS streaming (eleven_v3 streaming is REST-only, rejects WS with 403).
    # Turbo trades clarity for ~100ms latency; for a receptionist call where
    # the caller has to understand every word, the quality is worth it.
    elevenlabs_model_id: str = "eleven_multilingual_v2"

    # --- Telnyx ---
    telnyx_api_key: str
    telnyx_public_key: str | None = None  # used to verify webhook signatures
    telnyx_phone_number: str | None = None
    telnyx_connection_id: str | None = None

    # --- Google (optional for the streaming-conversation milestone) ---
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_refresh_token: str | None = None
    google_calendar_id: str | None = None

    # --- App ---
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_base_url: str | None = None  # public URL of the app (Cloudflare tunnel)
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./feli_voice_agent.db"

    # Outbound call feedback spool. The webhook handler appends one JSON line
    # per `call.hangup` event here; tools/dialer.py drains it into the sheet on
    # the next run. Default sits next to the OpenClaw wrapper so it's covered
    # by the same TCC grant on macOS.
    hangup_spool_path: str = "~/Library/Application Support/felicetti-voice/hangups.jsonl"

    # --- Derived ---
    # Filled in by __post_init__ via object.__setattr__ since the dataclass is frozen.
    media_ws_url: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.app_base_url:
            # Strip trailing slash and convert https:// -> wss:// for the media WS.
            base = self.app_base_url.rstrip("/")
            if base.startswith("https://"):
                ws_base = "wss://" + base[len("https://") :]
            elif base.startswith("http://"):
                ws_base = "ws://" + base[len("http://") :]
            else:
                ws_base = base
            object.__setattr__(self, "media_ws_url", f"{ws_base}/telnyx/media")

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            anthropic_api_key=_require("ANTHROPIC_API_KEY"),
            anthropic_model=_env("ANTHROPIC_MODEL") or "claude-sonnet-4-6",
            deepgram_api_key=_require("DEEPGRAM_API_KEY"),
            deepgram_model=_env("DEEPGRAM_MODEL") or "nova-3",
            deepgram_language=_env("DEEPGRAM_LANGUAGE") or "multi",
            elevenlabs_api_key=_require("ELEVENLABS_API_KEY"),
            elevenlabs_voice_id=_require("ELEVENLABS_VOICE_ID"),
            elevenlabs_model_id=_env("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2",
            telnyx_api_key=_require("TELNYX_API_KEY"),
            telnyx_public_key=_env("TELNYX_PUBLIC_KEY"),
            telnyx_phone_number=_env("TELNYX_PHONE_NUMBER"),
            telnyx_connection_id=_env("TELNYX_CONNECTION_ID"),
            google_client_id=_env("GOOGLE_CLIENT_ID"),
            google_client_secret=_env("GOOGLE_CLIENT_SECRET"),
            google_refresh_token=_env("GOOGLE_REFRESH_TOKEN"),
            google_calendar_id=_env("GOOGLE_CALENDAR_ID"),
            app_host=_env("APP_HOST") or "0.0.0.0",
            app_port=int(_env("APP_PORT") or "8000"),
            app_base_url=_env("APP_BASE_URL"),
            log_level=_env("LOG_LEVEL") or "INFO",
            database_url=_env("DATABASE_URL")
            or "sqlite+aiosqlite:///./feli_voice_agent.db",
            hangup_spool_path=_env("HANGUP_SPOOL_PATH")
            or "~/Library/Application Support/felicetti-voice/hangups.jsonl",
        )


# Module-level singleton. Imported as `from app.config import settings`.
settings = Settings.load()
