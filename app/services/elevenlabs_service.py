"""ElevenLabs streaming text-to-speech client.

Opens a WebSocket to ElevenLabs' input-streaming TTS endpoint. The orchestrator
sends text chunks as Claude produces them; the WS streams back μ-law 8kHz audio
chunks ready to feed into Telnyx Media Streaming.

ElevenLabs WS contract:
    - Open: send `{"text": " ", "voice_settings": {...}, "xi_api_key": "..."}` to init.
      (The leading space is required; ElevenLabs uses it as a "begin" sentinel.)
    - Then send `{"text": "more text"}` per chunk.
    - To flush: `{"text": ""}` with `flush: true`.
    - Server replies with `{"audio": "<base64>", "isFinal": bool, "normalizedAlignment": ...}`.

We request μ-law 8kHz output via the `output_format` query parameter so the
audio can be base64-encoded and shoved straight into Telnyx Media Streaming.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

import websockets
from websockets.asyncio.client import ClientConnection, connect as ws_connect

from app.config import settings

logger = logging.getLogger(__name__)

_TTS_BASE = "wss://api.elevenlabs.io/v1/text-to-speech"

AudioCallback = Callable[[bytes], Awaitable[None]]
# (mulaw_audio_bytes) -> None


class ElevenLabsStream:
    """Async context manager for one TTS WebSocket session.

    A session corresponds to one assistant turn. Open it, feed it text chunks
    as Claude streams, then close it (or call `flush()` to force final audio).
    """

    def __init__(
        self,
        on_audio: AudioCallback,
        *,
        api_key: str | None = None,
        voice_id: str | None = None,
        model_id: str | None = None,
        stability: float = 0.7,
        similarity_boost: float = 0.8,
        style: float = 0.0,
        use_speaker_boost: bool = True,
    ) -> None:
        self._api_key = api_key or settings.elevenlabs_api_key
        self._voice_id = voice_id or settings.elevenlabs_voice_id
        self._model_id = model_id or settings.elevenlabs_model_id
        self._on_audio = on_audio
        # Higher stability + speaker_boost smooths out the "robotic" feel on
        # phone audio. style=0 keeps delivery flat/professional (appropriate
        # for a receptionist; non-zero introduces emotional variation).
        self._voice_settings = {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": use_speaker_boost,
        }

        params = {
            "model_id": self._model_id,
            "output_format": "ulaw_8000",
            "inactivity_timeout": 60,
        }
        self._url = (
            f"{_TTS_BASE}/{self._voice_id}/stream-input?{urlencode(params)}"
        )
        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        # Set when ElevenLabs signals isFinal=true (all audio for the current
        # input has been delivered). Callers should await this instead of
        # sleeping a fixed duration after flush.
        self._final = asyncio.Event()

    async def __aenter__(self) -> "ElevenLabsStream":
        # Use the new asyncio client explicitly for consistency with the
        # rest of the codebase (also avoids legacy/new mismatch surprises).
        self._ws = await ws_connect(self._url, max_size=None)
        # Init message — leading space is the documented sentinel
        await self._ws.send(
            json.dumps(
                {
                    "text": " ",
                    "voice_settings": self._voice_settings,
                    "xi_api_key": self._api_key,
                }
            )
        )
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.debug("ElevenLabs stream opened for voice %s", self._voice_id)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def send_text(self, chunk: str) -> None:
        """Feed a chunk of text into the TTS stream."""
        if self._closed or self._ws is None or not chunk:
            return
        try:
            await self._ws.send(json.dumps({"text": chunk}))
        except Exception as e:  # noqa: BLE001
            logger.warning("ElevenLabs send failed: %s", e)

    async def flush(self) -> None:
        """Force generation of any buffered text and signal end-of-input.

        Two messages on purpose: `flush:true` forces generation of partial
        buffered text (otherwise ElevenLabs waits for more), and the second
        empty `text` (without flush) signals that no more input is coming.
        Without the second message ElevenLabs never emits `isFinal:true`,
        which makes `wait_for_final` time out and blocks the next turn.
        """
        if self._closed or self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({"text": "", "flush": True}))
            await self._ws.send(json.dumps({"text": ""}))
        except Exception:  # noqa: BLE001
            pass

    async def wait_for_final(self, timeout: float = 15.0) -> bool:
        """Wait until ElevenLabs signals isFinal=true after a flush.

        Returns True if the signal arrived in time, False if timed out.
        Callers should use this instead of a fixed `asyncio.sleep()` so that
        long inputs (which take longer to synthesize) aren't truncated.
        """
        try:
            await asyncio.wait_for(self._final.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "ElevenLabs isFinal not received within %.1fs", timeout
            )
            return False

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
        logger.debug("ElevenLabs stream closed")

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

                audio_b64 = msg.get("audio")
                if audio_b64:
                    try:
                        audio = base64.b64decode(audio_b64)
                    except Exception:  # noqa: BLE001
                        audio = None
                    if audio:
                        try:
                            await self._on_audio(audio)
                        except Exception:  # noqa: BLE001
                            logger.exception("on_audio callback raised")
                elif "audio" not in msg and "isFinal" not in msg:
                    # Genuinely unexpected message (e.g. error). Audio-less
                    # final-markers are normal and shouldn't warn.
                    logger.warning("ElevenLabs non-audio message: %s", msg)

                if msg.get("isFinal"):
                    self._final.set()
        except websockets.ConnectionClosed:
            logger.debug("ElevenLabs WS closed by server")
        except Exception:  # noqa: BLE001
            logger.exception("ElevenLabs receive loop failed")
        finally:
            # Unblock anyone awaiting wait_for_final() if the loop exits
            # without an isFinal message (e.g. server closed early).
            self._final.set()
