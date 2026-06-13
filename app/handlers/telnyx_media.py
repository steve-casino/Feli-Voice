"""Telnyx Media Streaming WebSocket handler + per-call conversation orchestrator.

When Telnyx answers a call with bidirectional Media Streaming enabled, it dials
this WebSocket. The protocol is JSON-over-WebSocket with a few event types:

    {"event": "connected", ...}
    {"event": "start", "start": {"call_control_id": "...", ...}, "stream_id": "..."}
    {"event": "media", "media": {"payload": "<base64 μ-law>", "track": "inbound"}}
    {"event": "stop", ...}

We pipe inbound audio into Deepgram; when Deepgram emits a final transcript we
hand it to Claude; Claude's text deltas stream into ElevenLabs; ElevenLabs'
μ-law output is base64'd and shipped back over the same Telnyx WS as outbound
media frames.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import state
from app.config import settings
from app.models.telnyx import CallSession, TelnyxMediaFrame
from app.prompts.persona import (
    INTAKE_SUMMARY_PROMPT,
    INTAKE_SYSTEM_PROMPT_EN,
    INTAKE_SYSTEM_PROMPT_ES,
    LANGUAGE_CONFIRM_EN,
    LANGUAGE_CONFIRM_ES,
    LANGUAGE_GATE_GREETING,
    LANGUAGE_GATE_REPROMPT,
    OUTBOUND_GREETING_TEXT,
    OUTBOUND_SYSTEM_PROMPT,
)
from app.services import intake_spool
from app.services.anthropic_service import anthropic_service
from app.services.deepgram_service import DeepgramStream, keyterms_for_language
from app.services.elevenlabs_service import ElevenLabsStream

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/telnyx", tags=["telnyx"])

# Sentence-ending punctuation, incl. Spanish. We flush Claude's streamed text
# to ElevenLabs one full sentence at a time (rather than per-token) so TTS is
# paced at natural prosodic boundaries and ElevenLabs isn't re-flushing partial
# buffers on every delta.
_SENTENCE_ENDERS = ".!?…"
_SENTENCE_CLOSERS = "\"')]»”"

# Trim conversation history once it grows past this many messages, keeping the
# most recent few exchanges. The system prompt is sent separately (and cached)
# so it is never part of this list and is always retained.
_PRUNE_AFTER_MESSAGES = 12
_KEEP_MESSAGES = 8


def _drain_sentences(buffer: str) -> tuple[list[str], str]:
    """Split `buffer` into completed sentences + a trailing remainder.

    A boundary is sentence-ending punctuation (optionally followed by a closing
    quote/bracket) that is itself followed by whitespace. Punctuation at the
    very end of the buffer is left in the remainder so mid-stream numbers like
    "3.5" or an as-yet-unfinished sentence aren't flushed prematurely.
    """
    sentences: list[str] = []
    last = 0
    i = 0
    n = len(buffer)
    while i < n:
        if buffer[i] in _SENTENCE_ENDERS:
            j = i + 1
            while j < n and buffer[j] in _SENTENCE_CLOSERS:
                j += 1
            if j < n and buffer[j].isspace():
                while j < n and buffer[j].isspace():
                    j += 1
                sentences.append(buffer[last:j])
                last = j
                i = j
                continue
        i += 1
    return sentences, buffer[last:]

# ---------------------------------------------------------------------------
# Language-gate classification
# ---------------------------------------------------------------------------
# The greeting asks the caller to say "English" (asked in English) or
# "español" (asked in Spanish). Classification is deliberately deterministic —
# no LLM round-trip for a two-way choice — with loose hint matching as a
# fallback for callers who answer with a sentence instead of one word.

_ES_CHOICE_WORDS = {"español", "espanol", "spanish", "castellano"}
_EN_CHOICE_WORDS = {"english", "inglés", "ingles"}
_ES_HINT_WORDS = {
    "hola", "buenas", "noches", "sí", "si", "quiero", "necesito", "hablo",
    "habla", "favor", "ayuda", "llamo", "abogado", "gracias", "bueno",
}
_EN_HINT_WORDS = {
    "hello", "hi", "hey", "yes", "yeah", "please", "help", "need", "calling",
    "call", "lawyer", "attorney", "speak", "want",
}

_WORD_RE = re.compile(r"[a-záéíóúüñ]+")


def classify_language_choice(text: str) -> str | None:
    """Map a caller's first utterance to "en"/"es", or None if unclear.

    Explicit choice words win outright; saying "Spanish" in English still
    means the caller wants Spanish. If both appear (caller echoing the whole
    prompt), it's ambiguous. Otherwise fall back to counting common-word
    hints in each language.
    """
    tokens = set(_WORD_RE.findall(text.lower()))
    wants_es = bool(tokens & _ES_CHOICE_WORDS)
    wants_en = bool(tokens & _EN_CHOICE_WORDS)
    if wants_es and not wants_en:
        return "es"
    if wants_en and not wants_es:
        return "en"
    if wants_en and wants_es:
        return None
    es_hits = len(tokens & _ES_HINT_WORDS)
    en_hits = len(tokens & _EN_HINT_WORDS)
    if es_hits > en_hits:
        return "es"
    if en_hits > es_hits:
        return "en"
    return None


@router.websocket("/media")
async def telnyx_media(ws: WebSocket) -> None:
    await ws.accept()
    logger.info("Telnyx media WS accepted")

    session: CallSession | None = None
    orchestrator: ConversationOrchestrator | None = None

    try:
        async for raw in ws.iter_text():
            try:
                frame = TelnyxMediaFrame.model_validate_json(raw)
            except Exception as e:  # noqa: BLE001
                logger.debug("Unparseable frame: %s", e)
                continue

            if frame.event == "connected":
                continue

            if frame.event == "start":
                start = frame.start or {}
                call_control_id = (
                    start.get("call_control_id")
                    or start.get("callControlId")
                    or "unknown"
                )
                logger.info(
                    "Telnyx stream started: stream_id=%s call=%s start_payload=%s",
                    frame.stream_id,
                    call_control_id,
                    start,
                )

                # Check if this is an outbound call we initiated. Pop the
                # entry so it doesn't linger after the call ends.
                outbound_meta = state.outbound_calls.pop(call_control_id, None)
                direction = "outgoing" if outbound_meta is not None else "incoming"
                custom_greeting = outbound_meta.get("greeting") if outbound_meta else None

                session = CallSession(
                    call_control_id=call_control_id,
                    stream_id=frame.stream_id,
                    direction=direction,
                    from_number=start.get("from"),
                    to_number=start.get("to"),
                )
                orchestrator = ConversationOrchestrator(
                    ws=ws,
                    session=session,
                    custom_greeting=custom_greeting,
                )
                await orchestrator.start()
                continue

            if frame.event == "media" and orchestrator is not None:
                if frame.media and frame.media.payload:
                    # Telnyx can deliver both inbound and outbound tracks on
                    # the stream. Only the caller's inbound audio should be
                    # sent to Deepgram, otherwise our own TTS gets
                    # transcribed and the model starts talking to itself.
                    if frame.media.track not in (None, "inbound"):
                        continue
                    try:
                        audio = base64.b64decode(frame.media.payload)
                    except Exception:  # noqa: BLE001
                        continue
                    await orchestrator.on_inbound_audio(audio)
                continue

            if frame.event == "stop":
                logger.info("Telnyx stream stopped")
                break

    except WebSocketDisconnect:
        logger.info("Telnyx media WS disconnected")
    except Exception:  # noqa: BLE001
        logger.exception("Media WS loop crashed")
    finally:
        if orchestrator is not None:
            await orchestrator.aclose()
            # Persist the intake record (inbound calls only) after teardown,
            # while this per-connection task is still alive.
            await orchestrator.save_intake_if_needed()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Conversation orchestrator
# ---------------------------------------------------------------------------


class ConversationOrchestrator:
    """Glues Deepgram, Claude, and ElevenLabs together for one call.

    Lifecycle:
        start()                — open Deepgram, speak greeting
        on_inbound_audio(buf)  — forward to Deepgram
        (Deepgram emits final transcripts -> _run_assistant_turn)
        aclose()               — tear everything down
    """

    # μ-law 8kHz mono → 8000 bytes/sec → 160 bytes = 20ms.
    # Telnyx Media Streaming expects these small, real-time-paced chunks;
    # large blobs play poorly (or not at all) mid-call.
    _OUTBOUND_CHUNK_BYTES = 160
    _OUTBOUND_FRAME_INTERVAL = 0.02  # seconds between paced sends

    def __init__(
        self,
        ws: WebSocket,
        session: CallSession,
        *,
        custom_greeting: str | None = None,
    ) -> None:
        self._ws = ws
        self._session = session
        self._dg: DeepgramStream | None = None
        self._turn_lock = asyncio.Lock()
        self._closed = False
        self._assistant_turn_task: asyncio.Task | None = None
        self._assistant_turn_requested = asyncio.Event()
        # The currently-speaking assistant turn, tracked so a barge-in can
        # cancel it mid-reply. Distinct from the long-lived turn *loop*.
        self._active_turn_task: asyncio.Task | None = None
        # Background greeting playback (fired in start()). Tracked so the
        # language-confirm reply or first assistant turn can stop it before
        # speaking, instead of queuing audio on top of a still-playing greeting.
        self._greeting_task: asyncio.Task | None = None
        # Set when the caller has produced final transcript text that hasn't
        # been answered yet. Gates the turn so UtteranceEnd/speech_final don't
        # fire empty turns.
        self._pending_user_input = False
        # Diagnostics
        self._inbound_frames = 0
        self._outbound_frames = 0
        # Outbound pacing: enqueue 160-byte μ-law chunks; a consumer task
        # dequeues them and ships one to Telnyx every 20ms.
        self._out_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._pacer_task: asyncio.Task | None = None
        # Carryover for partial frames between ElevenLabs audio messages.
        # ElevenLabs emits buffers of arbitrary length; without this, every
        # non-160-aligned message would produce one short (<20ms) frame and
        # the pacer's 20ms slot would leave a gap of silence — audible chop.
        self._outbound_partial = bytearray()

        # Full conversation log for the intake record. Unlike
        # session.transcript (pruned for token cost, cleared at the language
        # gate), this keeps every line of the call.
        self._call_log: list[dict] = []
        # Caller's locked language ("en"/"es"), set at the language gate.
        self._language: str | None = None
        self._lang_gate_attempts = 0

        # Select greeting, system prompt, and flow stage by call direction.
        # Inbound calls open with a language gate: the caller picks English
        # or Spanish, then the call proceeds in that language only.
        if session.direction == "outgoing":
            self._greeting = custom_greeting or OUTBOUND_GREETING_TEXT
            self._system_prompt = OUTBOUND_SYSTEM_PROMPT
            self._stage = "conversation"
        else:
            self._greeting = LANGUAGE_GATE_GREETING
            # Replaced with the locked-language intake persona at the gate;
            # EN fallback in case a turn somehow runs before selection.
            self._system_prompt = INTAKE_SYSTEM_PROMPT_EN
            self._stage = "language_select"

    async def start(self) -> None:
        logger.info("Orchestrator starting; opening Deepgram stream")
        self._dg = DeepgramStream(
            on_transcript=self._on_transcript,
            on_utterance_end=self._on_utterance_end,
        )
        await self._dg.__aenter__()
        logger.info("Deepgram stream opened")

        # Start the outbound pacer so audio chunks are shipped in real time.
        self._pacer_task = asyncio.create_task(self._outbound_pacer())

        # Seed conversation history with the greeting as the first assistant
        # turn, then actually speak it.
        logger.info(
            "Call direction=%s; greeting=%r",
            self._session.direction,
            self._greeting,
        )
        self._session.transcript.append(
            {"role": "assistant", "content": self._greeting}
        )
        self._call_log.append({"role": "assistant", "content": self._greeting})
        self._assistant_turn_task = asyncio.create_task(self._assistant_turn_loop())
        self._greeting_task = asyncio.create_task(self._speak(self._greeting))

    async def on_inbound_audio(self, mulaw_bytes: bytes) -> None:
        self._inbound_frames += 1
        if self._inbound_frames in (1, 10, 50) or self._inbound_frames % 200 == 0:
            logger.info(
                "Inbound media frames received from Telnyx: %d (last frame %d bytes)",
                self._inbound_frames,
                len(mulaw_bytes),
            )
        if self._dg is not None:
            await self._dg.send_audio(mulaw_bytes)

    async def _on_transcript(
        self, text: str, is_final: bool, speech_final: bool
    ) -> None:
        text = text.strip()
        if not text:
            return

        if not is_final:
            # Interim hypothesis = the caller is talking right now. If the
            # agent is mid-reply, cancel it so our TTS doesn't bleed into the
            # caller's audio (acoustic crosstalk wrecks the next transcript).
            if len(text) >= 2:
                self._handle_barge_in()
            return

        logger.info("Caller (final): %s", text)
        if self._call_log and self._call_log[-1]["role"] == "user":
            self._call_log[-1]["content"] += " " + text
        else:
            self._call_log.append({"role": "user", "content": text})
        if self._session.transcript and self._session.transcript[-1]["role"] == "user":
            # Concatenate consecutive user finals (Deepgram emits multiple
            # finals per long utterance).
            self._session.transcript[-1]["content"] += " " + text
        else:
            self._session.transcript.append({"role": "user", "content": text})
        self._pending_user_input = True
        # speech_final means endpointing detected end-of-speech; respond now.
        # Otherwise wait for UtteranceEnd as the turn boundary.
        if speech_final:
            self._maybe_request_turn()

    async def _on_utterance_end(self) -> None:
        # Deepgram's silence-based end-of-turn marker; reliable backstop when
        # endpointing didn't emit a speech_final (noisy line, no clear pause).
        self._maybe_request_turn()

    def _maybe_request_turn(self) -> None:
        if self._pending_user_input and not self._closed:
            self._assistant_turn_requested.set()

    def _handle_barge_in(self) -> None:
        """Caller spoke over the agent — stop the in-flight reply immediately.

        Only acts while an assistant turn is actually speaking; the greeting
        and idle periods are left untouched so background noise can't cut off
        the opening line.
        """
        if self._active_turn_task is None or self._active_turn_task.done():
            return
        self._active_turn_task.cancel()
        self._drain_outbound_queue()
        logger.info("Barge-in: cancelled in-flight assistant turn")

    def _drain_outbound_queue(self) -> None:
        """Drop all queued + partial outbound audio so the agent goes silent."""
        try:
            while True:
                self._out_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self._outbound_partial.clear()

    async def _cancel_greeting(self) -> None:
        """Ensure the opening greeting is finished before any other speech.

        The greeting is fired as a background task in start(); without this a
        language-confirm line or assistant turn would queue its audio on top of
        the still-playing greeting and the two would interleave. Cancel the task
        if it's still running (otherwise just await its completion), then drain
        any greeting audio still buffered or queued so the next speech is clean.
        Idempotent: subsequent calls are no-ops.
        """
        task = self._greeting_task
        if task is None:
            return
        self._greeting_task = None
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._drain_outbound_queue()

    async def _assistant_turn_loop(self) -> None:
        """Serialize assistant turns and keep up with interrupted callers.

        Deepgram can emit a new final transcript while the assistant is still
        speaking. Rather than dropping that follow-up utterance, keep one
        background loop alive for the life of the call and wake it whenever a
        new user turn arrives.
        """
        try:
            while not self._closed:
                await self._assistant_turn_requested.wait()
                self._assistant_turn_requested.clear()
                if self._closed:
                    return
                async with self._turn_lock:
                    self._pending_user_input = False
                    # Language gate: the first caller utterance(s) pick the
                    # call language. Handled inline (not as a cancellable
                    # child task) so a barge-in can't abort the language
                    # swap halfway through.
                    if self._stage == "language_select":
                        await self._handle_language_selection()
                        continue
                    # Run the turn as a child task so a barge-in can cancel it
                    # mid-reply without tearing down this loop.
                    self._active_turn_task = asyncio.create_task(
                        self._run_assistant_turn_once()
                    )
                    try:
                        await self._active_turn_task
                    except asyncio.CancelledError:
                        if self._closed:
                            raise
                        # Barge-in cancelled this turn; keep serving the call.
                    finally:
                        self._active_turn_task = None
        except asyncio.CancelledError:
            return

    async def _handle_language_selection(self) -> None:
        """Resolve the caller's language choice from their latest utterance.

        Unclear answer → one fixed bilingual reprompt; still unclear → default
        to English (an English-speaking agent asking for a callback number is
        recoverable in any case; dead air is not).
        """
        # Stop the opening greeting before the reprompt/confirm so fixed-line
        # speech never overlaps.
        await self._cancel_greeting()
        text = ""
        if self._session.transcript and self._session.transcript[-1]["role"] == "user":
            text = self._session.transcript[-1]["content"]
        lang = classify_language_choice(text)
        logger.info("Language gate: heard %r -> %s", text, lang)
        if lang is None:
            self._lang_gate_attempts += 1
            if self._lang_gate_attempts < 2:
                self._session.transcript.append(
                    {"role": "assistant", "content": LANGUAGE_GATE_REPROMPT}
                )
                self._call_log.append(
                    {"role": "assistant", "content": LANGUAGE_GATE_REPROMPT}
                )
                await self._speak(LANGUAGE_GATE_REPROMPT)
                return
            lang = "en"
            logger.info("Language gate: still unclear, defaulting to English")
        await self._lock_language(lang)

    async def _lock_language(self, lang: str) -> None:
        """Switch the call into single-language mode.

        Swaps in the locked-language intake persona, restarts STT as a
        monolingual stream (markedly more accurate than multi mode), resets
        the LLM conversation so the gate exchange doesn't bleed into intake
        context, and speaks the confirmation + first intake question.
        """
        self._stage = "conversation"
        self._language = lang
        self._system_prompt = (
            INTAKE_SYSTEM_PROMPT_ES if lang == "es" else INTAKE_SYSTEM_PROMPT_EN
        )
        confirm = LANGUAGE_CONFIRM_ES if lang == "es" else LANGUAGE_CONFIRM_EN
        # Preserve the caller's last utterance before resetting history — it
        # often carries the real reason for the call ("necesito un abogado por
        # un accidente"), not just the language word. Re-append it after the
        # confirm line so Claude's first real turn has that context.
        preserved_user: str | None = None
        for msg in reversed(self._session.transcript):
            if msg["role"] == "user":
                preserved_user = msg["content"]
                break
        # Fresh LLM history: seed with the confirmation line so Claude knows
        # it already asked for the caller's name.
        self._session.transcript.clear()
        self._session.transcript.append({"role": "assistant", "content": confirm})
        if preserved_user:
            self._session.transcript.append(
                {"role": "user", "content": preserved_user}
            )
        self._call_log.append({"role": "assistant", "content": confirm})
        logger.info("Language locked: %s", lang)
        await self._swap_deepgram(lang)
        await self._speak(confirm)

    async def _swap_deepgram(self, lang: str) -> None:
        """Replace the multilingual STT stream with a monolingual one.

        Opens the new stream before closing the old one so a failure (e.g.
        unsupported language/model combination) degrades gracefully back to
        the multilingual stream instead of leaving the call deaf.
        """
        try:
            new_dg = DeepgramStream(
                on_transcript=self._on_transcript,
                on_utterance_end=self._on_utterance_end,
                language=lang,
                # Monolingual stream: the longer 300ms endpoint is more
                # accurate than the 100ms used for the multilingual gate.
                endpointing_ms=300,
                keyterms=keyterms_for_language(lang),
            )
            await new_dg.__aenter__()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Deepgram %s swap failed; keeping multilingual stream", lang
            )
            return
        old_dg, self._dg = self._dg, new_dg
        if old_dg is not None:
            try:
                await old_dg.close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("Deepgram stream swapped to language=%s", lang)

    async def save_intake_if_needed(self) -> None:
        """Write the intake record for inbound calls. Called at teardown.

        Skips outbound calls (the dialer's hangup spool covers those) and
        calls where the caller never said anything. Summary generation is
        best-effort: on failure the raw transcript still gets stored.
        """
        if self._session.direction != "incoming":
            return
        if not any(m["role"] == "user" for m in self._call_log):
            logger.info("No caller speech; skipping intake record")
            return
        summary: str | None = None
        try:
            convo = "\n".join(
                f"{m['role']}: {m['content']}" for m in self._call_log
            )
            parts: list[str] = []
            async for delta in anthropic_service.stream_reply(
                system=INTAKE_SUMMARY_PROMPT,
                messages=[{"role": "user", "content": convo}],
                max_tokens=300,
                temperature=0.0,
            ):
                parts.append(delta)
            summary = "".join(parts).strip() or None
        except Exception:  # noqa: BLE001
            logger.exception("Intake summary generation failed; storing raw only")
        await intake_spool.append_intake(
            spool_path=settings.intake_spool_path,
            call_control_id=self._session.call_control_id,
            from_number=self._session.from_number,
            to_number=self._session.to_number,
            language=self._language,
            started_at=self._session.started_at,
            transcript=self._call_log,
            summary=summary,
        )

    async def _run_assistant_turn_once(self) -> None:
        # Only one assistant turn at a time. If user speaks while we're
        # generating, we'll catch it on the next final.
        try:
            # Never let the greeting's audio bleed into a real reply.
            await self._cancel_greeting()
            self._prune_transcript()
            full_reply: list[str] = []
            pending = ""  # text buffered until a full sentence is ready

            async def on_audio(audio: bytes) -> None:
                await self._send_outbound_audio(audio)

            async with ElevenLabsStream(on_audio=on_audio) as tts:
                async for delta in anthropic_service.stream_reply(
                    system=self._system_prompt,
                    messages=self._session.transcript,
                ):
                    full_reply.append(delta)
                    pending += delta
                    sentences, pending = _drain_sentences(pending)
                    for sentence in sentences:
                        await tts.send_text(sentence)
                if pending.strip():
                    await tts.send_text(pending)
                await tts.flush()
                # Wait until ElevenLabs signals it has delivered every
                # audio chunk. Fixed sleeps here truncate longer replies.
                await tts.wait_for_final(timeout=20.0)
            await self._flush_outbound()

            reply_text = "".join(full_reply).strip()
            if reply_text:
                self._session.transcript.append(
                    {"role": "assistant", "content": reply_text}
                )
                self._call_log.append(
                    {"role": "assistant", "content": reply_text}
                )
                logger.info("Agent: %s", reply_text)
        except Exception:  # noqa: BLE001
            logger.exception("Assistant turn failed")

    def _prune_transcript(self) -> None:
        """Cap conversation history so per-turn token cost stays bounded.

        Keeps the last `_KEEP_MESSAGES` messages. The system prompt is sent
        separately and cached, so it is always retained. The Anthropic API
        requires the first message to be a user turn, so a leading assistant
        message (e.g. the greeting) is dropped after slicing.
        """
        t = self._session.transcript
        if len(t) <= _PRUNE_AFTER_MESSAGES:
            return
        del t[: len(t) - _KEEP_MESSAGES]
        if t and t[0]["role"] != "user":
            del t[0]

    async def _speak(self, text: str) -> None:
        """One-shot TTS for greeting / fixed lines."""
        logger.info("Greeting TTS starting: %r", text)
        try:
            async def on_audio(audio: bytes) -> None:
                await self._send_outbound_audio(audio)

            async with ElevenLabsStream(on_audio=on_audio) as tts:
                logger.info("ElevenLabs stream opened for greeting")
                await tts.send_text(text)
                await tts.flush()
                await tts.wait_for_final(timeout=20.0)
            await self._flush_outbound()
            logger.info(
                "Greeting TTS complete (sent %d outbound frames to Telnyx)",
                self._outbound_frames,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Greeting TTS failed")

    async def _send_outbound_audio(self, mulaw_bytes: bytes) -> None:
        """Slice an ElevenLabs audio buffer into 20ms μ-law chunks and
        enqueue them. The pacer task ships them to Telnyx one at a time.

        Partial trailing bytes are carried over to the next call so every
        emitted frame is exactly 160 bytes (= 20ms at μ-law 8kHz).
        """
        if self._closed or not mulaw_bytes:
            return
        self._outbound_partial.extend(mulaw_bytes)
        size = self._OUTBOUND_CHUNK_BYTES
        while len(self._outbound_partial) >= size:
            chunk = bytes(self._outbound_partial[:size])
            del self._outbound_partial[:size]
            await self._out_queue.put(chunk)

    async def _flush_outbound(self) -> None:
        """Pad any sub-frame carryover with μ-law silence (0xFF) and emit.

        Called at end-of-turn so the last <20ms of audio isn't dropped.
        """
        if self._closed:
            return
        size = self._OUTBOUND_CHUNK_BYTES
        if 0 < len(self._outbound_partial) < size:
            self._outbound_partial.extend(
                b"\xff" * (size - len(self._outbound_partial))
            )
            chunk = bytes(self._outbound_partial)
            self._outbound_partial.clear()
            await self._out_queue.put(chunk)

    async def _outbound_pacer(self) -> None:
        """Drain the outbound queue at real-time cadence (1 frame / 20ms).

        Uses an *absolute* monotonic clock so per-frame send latency doesn't
        accumulate. With naive `await send(); await sleep(0.02)` each cycle
        takes `send_time + 20ms`, drifting audio ~10–25% slow and choppy
        over a few seconds. Here we compute `next_send_time` ahead of the
        actual send so the loop self-corrects.
        """
        try:
            next_send_time: float | None = None
            interval = self._OUTBOUND_FRAME_INTERVAL
            while not self._closed:
                chunk = await self._out_queue.get()
                if chunk is None:  # poison pill from aclose()
                    break
                now = time.monotonic()
                # Reset cadence if we've been idle for a while (e.g. between
                # agent turns) so we don't burst-send a backlog.
                if next_send_time is None or now > next_send_time + 0.5:
                    next_send_time = now
                if now < next_send_time:
                    await asyncio.sleep(next_send_time - now)
                await self._send_frame(chunk)
                next_send_time += interval
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("Outbound pacer crashed")

    async def _send_frame(self, mulaw_chunk: bytes) -> None:
        """Ship one paced μ-law frame to Telnyx."""
        if self._closed:
            return
        try:
            payload = base64.b64encode(mulaw_chunk).decode("ascii")
            # Telnyx requires the stream_id from the "start" event to be
            # echoed in every outbound media frame, otherwise audio is
            # silently dropped. Including chunk (monotonic sequence) and
            # timestamp (ms since stream start) lets Telnyx use our pacing
            # instead of arrival-time, which removes jitter-driven choppiness.
            self._outbound_frames += 1
            timestamp_ms = self._outbound_frames * 20  # 20ms per μ-law frame
            frame: dict = {
                "event": "media",
                "media": {
                    "payload": payload,
                    "chunk": self._outbound_frames,
                    "timestamp": str(timestamp_ms),
                },
            }
            if self._session.stream_id:
                frame["stream_id"] = self._session.stream_id
            await self._ws.send_text(json.dumps(frame))
            if (
                self._outbound_frames in (1, 50, 200)
                or self._outbound_frames % 500 == 0
            ):
                logger.info(
                    "Outbound media frames sent to Telnyx: %d (last %d bytes raw, queue depth %d)",
                    self._outbound_frames,
                    len(mulaw_chunk),
                    self._out_queue.qsize(),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("Outbound media send failed: %s", e)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._assistant_turn_requested.set()
        # Cancel any in-flight reply first so it doesn't outlive the loop.
        if self._active_turn_task is not None:
            self._active_turn_task.cancel()
        if self._assistant_turn_task is not None:
            self._assistant_turn_task.cancel()
            try:
                await self._assistant_turn_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Signal the pacer to drain & exit. Wait briefly so any tail audio
        # actually reaches Telnyx; cancel if it hangs.
        await self._out_queue.put(None)
        if self._pacer_task is not None:
            try:
                await asyncio.wait_for(self._pacer_task, timeout=1.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                self._pacer_task.cancel()
        if self._dg is not None:
            await self._dg.close()
