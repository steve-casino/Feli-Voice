"""Pydantic models for Telnyx webhook payloads and internal call state.

Telnyx webhook events are wrapped in an envelope: { "data": { "event_type": "call.initiated", "payload": {...} } }.
We model the envelope and a few specific payloads we actually act on. Other
event types pass through with an `extra="allow"` payload so we don't choke on
events we haven't modeled yet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Webhook envelopes
# ---------------------------------------------------------------------------


class TelnyxCallPayload(BaseModel):
    """Common fields present on most Telnyx call events.

    We allow extra fields so unmapped attributes (e.g. SIP headers, custom
    parameters) pass through without raising.
    """

    model_config = ConfigDict(extra="allow")

    call_control_id: str
    call_leg_id: str | None = None
    call_session_id: str | None = None
    connection_id: str | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    direction: Literal["incoming", "outgoing"] | None = None
    state: str | None = None
    client_state: str | None = None
    # Hangup-specific fields. Present only on `call.hangup` events; surfacing
    # them here keeps the webhook handler from poking into the raw envelope.
    hangup_cause: str | None = None
    hangup_source: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None


class TelnyxEventData(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_type: str
    id: str | None = None
    occurred_at: datetime | None = None
    record_type: str | None = None
    payload: TelnyxCallPayload


class TelnyxWebhookEnvelope(BaseModel):
    """Top-level shape Telnyx POSTs to our webhook."""

    model_config = ConfigDict(extra="allow")

    data: TelnyxEventData
    meta: dict | None = None


# ---------------------------------------------------------------------------
# Media Streaming WebSocket frames
# ---------------------------------------------------------------------------


class TelnyxMediaPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Base64-encoded μ-law 8kHz audio (when event == "media")
    payload: str | None = None
    track: Literal["inbound", "outbound"] | None = None
    chunk: int | None = None
    timestamp: str | None = None


class TelnyxMediaFrame(BaseModel):
    """Individual JSON frame on the Telnyx Media Streaming WebSocket.

    event values seen in practice:
      - "connected"  : socket established
      - "start"      : streaming about to begin; includes call metadata
      - "media"      : an audio chunk
      - "stop"       : streaming over (call ended or stream stopped)
      - "mark"       : echoed back when we send a mark frame outbound
    """

    model_config = ConfigDict(extra="allow")

    event: str
    sequence_number: int | None = None
    stream_id: str | None = None
    media: TelnyxMediaPayload | None = None
    start: dict | None = None
    stop: dict | None = None
    mark: dict | None = None


# ---------------------------------------------------------------------------
# Internal call-session state (held in-memory while a call is live)
# ---------------------------------------------------------------------------


class CallSession(BaseModel):
    """Per-call mutable state, shared between the webhook + media WS handlers."""

    model_config = ConfigDict(extra="allow")

    call_control_id: str
    call_leg_id: str | None = None
    from_number: str | None = None
    to_number: str | None = None
    started_at: datetime = Field(default_factory=datetime.utcnow)
    stream_id: str | None = None
    direction: Literal["incoming", "outgoing"] = "incoming"
    # Running conversation history in Anthropic message format
    transcript: list[dict] = Field(default_factory=list)
