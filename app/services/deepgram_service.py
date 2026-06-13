"""Deepgram streaming speech-to-text client.

Opens a WebSocket to Deepgram's `/v1/listen` streaming endpoint, accepts μ-law
8kHz audio frames (already decoded from Telnyx base64), and yields finalized
transcripts via an async callback.

Usage:
    async with DeepgramStream(on_final_transcript=handle) as dg:
        await dg.send_audio(mulaw_bytes)
        ...
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

import websockets
from websockets.asyncio.client import ClientConnection, connect as ws_connect

from app.config import settings
from app.prompts.persona import FIRM_NAME, PRACTICE_AREAS

logger = logging.getLogger(__name__)

_DEEPGRAM_WS = "wss://api.deepgram.com/v1/listen"

TranscriptCallback = Callable[[str, bool, bool], Awaitable[None]]
# (transcript_text, is_final, speech_final) -> None

UtteranceEndCallback = Callable[[], Awaitable[None]]
# Fired when Deepgram emits an UtteranceEnd event (true turn boundary).


def _en_keyterms() -> list[str]:
    """English domain vocabulary, sourced from persona so it stays in sync."""
    practice_areas = [
        t.strip() for t in re.split(r",|\band\b", PRACTICE_AREAS) if t.strip()
    ]
    return [
        FIRM_NAME,
        "Felicetti",
        "consultation",
        "attorney",
        "appointment",
        "intake",
        *practice_areas,
    ]


def _es_keyterms() -> list[str]:
    """Spanish equivalents for the formal "usted" law-firm register."""
    return [
        "Felicetti",
        "consulta",
        "abogado",
        "cita",
        "bufete",
        "lesión personal",
        "derecho de familia",
        "planificación patrimonial",
    ]


def keyterms_for_language(language: str | None) -> list[str]:
    """Keyterm boost list for a locked-language stream.

    `"en"`/`"es"` get their monolingual lists; anything else (e.g. `"multi"`)
    gets the combined list used during the bilingual language gate.
    """
    if language == "en":
        return _en_keyterms()
    if language == "es":
        return _es_keyterms()
    return _default_keyterms()


def _default_keyterms() -> list[str]:
    """Combined EN+ES vocabulary for the multilingual stream.

    Phone audio + multilingual mode mis-hears the firm name, attorney/legal
    terms, and practice areas most often.
    """
    return [*_en_keyterms(), *_es_keyterms()]


class DeepgramStream:
    """Async context manager wrapping a single streaming STT WebSocket."""

    def __init__(
        self,
        on_transcript: TranscriptCallback,
        *,
        on_utterance_end: UtteranceEndCallback | None = None,
        api_key: str | None = None,
        model: str | None = None,
        language: str | None = None,
        encoding: str = "mulaw",
        sample_rate: int = 8000,
        channels: int = 1,
        endpointing_ms: int | None = None,
        utterance_end_ms: int = 1000,
        keyterms: list[str] | None = None,
    ) -> None:
        self._api_key = api_key or settings.deepgram_api_key
        self._on_transcript = on_transcript
        self._on_utterance_end = on_utterance_end

        model_name = model or settings.deepgram_model
        lang = language or settings.deepgram_language
        # nova-3 uses `keyterm` (plain phrase); nova-2 uses `keywords`
        # (phrase:intensity). Branching on the model keeps a DEEPGRAM_MODEL
        # env override fully reversible without code changes.
        is_nova3 = model_name.startswith("nova-3")
        terms = keyterms if keyterms is not None else _default_keyterms()

        if endpointing_ms is None:
            # Deepgram's multilingual guidance recommends a short 100ms endpoint
            # for language=multi (code-switching finalizes faster); monolingual
            # streams transcribe more accurately with a longer 300ms window.
            endpointing_ms = 100 if lang == "multi" else 300

        params: list[tuple[str, str | int]] = [
            ("model", model_name),
            ("language", lang),
            ("encoding", encoding),
            ("sample_rate", sample_rate),
            ("channels", channels),
            ("punctuate", "true"),
            ("smart_format", "true"),
            ("numerals", "true"),
            ("interim_results", "true"),
            ("endpointing", endpointing_ms),
            # UtteranceEnd events give a reliable turn boundary across pauses
            # that endpointing alone can miss on noisy phone audio. Requires
            # interim_results=true (set above).
            ("utterance_end_ms", utterance_end_ms),
        ]
        for t in terms:
            if is_nova3:
                params.append(("keyterm", t))
            else:
                params.append(("keywords", f"{t}:2"))
        self._url = f"{_DEEPGRAM_WS}?{urlencode(params)}"
        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False

    async def __aenter__(self) -> "DeepgramStream":
        # Use the new asyncio client explicitly; the legacy `websockets.connect`
        # rejects `additional_headers` (its kwarg is `extra_headers`).
        self._ws = await ws_connect(
            self._url,
            additional_headers={"Authorization": f"Token {self._api_key}"},
            max_size=None,
        )
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.debug("Deepgram stream opened: %s", self._url)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def send_audio(self, frame: bytes) -> None:
        """Send a single μ-law audio frame to Deepgram. No-op if already closed."""
        if self._closed or self._ws is None:
            return
        try:
            await self._ws.send(frame)
        except Exception as e:  # noqa: BLE001
            logger.warning("Deepgram send failed: %s", e)

    async def finish(self) -> None:
        """Tell Deepgram we're done sending audio (flushes the last transcript)."""
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:  # noqa: BLE001
            pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        logger.debug("Deepgram stream closed")

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type")

                # UtteranceEnd is the true end-of-turn signal: Deepgram emits
                # it after `utterance_end_ms` of silence even when endpointing
                # didn't fire a speech_final. Use it to release the assistant
                # turn instead of relying solely on per-result finals.
                if msg_type == "UtteranceEnd":
                    if self._on_utterance_end is not None:
                        try:
                            await self._on_utterance_end()
                        except Exception:  # noqa: BLE001
                            logger.exception("on_utterance_end callback raised")
                    continue

                # Standard Deepgram transcript message:
                # { "type": "Results", "channel": { "alternatives": [{ "transcript": "..." }] }, "is_final": bool, "speech_final": bool }
                if msg_type != "Results":
                    continue

                alternatives = (msg.get("channel") or {}).get("alternatives") or []
                if not alternatives:
                    continue
                transcript = (alternatives[0] or {}).get("transcript", "").strip()
                if not transcript:
                    continue
                is_final = bool(msg.get("is_final"))
                speech_final = bool(msg.get("speech_final"))
                try:
                    await self._on_transcript(transcript, is_final, speech_final)
                except Exception:  # noqa: BLE001
                    logger.exception("on_transcript callback raised")
        except websockets.ConnectionClosed:
            logger.debug("Deepgram WS closed by server")
        except Exception:  # noqa: BLE001
            logger.exception("Deepgram receive loop failed")
