"""Outbound call initiator.

Exposes a single REST endpoint that triggers an outbound call via Telnyx.
The same Deepgram → Claude → ElevenLabs pipeline used for inbound calls
handles the conversation once the called party picks up.

Usage:
    POST /calls/outbound
    {
        "to": "+15551234567",
        "greeting": "Hi, this is Sarah calling from Felicetti Law Firm...", // optional
        "from_number": "+14047774002",  // optional, overrides TELNYX_PHONE_NUMBER
        "row": 5,                       // optional, sheet row for feedback loop
        "sheet_id": "1AbC..."           // optional, sheet identity for feedback loop
    }

If `row` (and optionally `sheet_id`) is set, that metadata is round-tripped to
Telnyx via `client_state`, so the `call.hangup` webhook can later attribute the
outcome back to the correct sheet row without any in-memory mapping.
"""

from __future__ import annotations

import base64
import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import state
from app.config import settings
from app.services.telnyx_service import TelnyxError, telnyx_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/calls", tags=["calls"])


class OutboundCallRequest(BaseModel):
    """Body for POST /calls/outbound."""

    to: str = Field(..., description="E.164 number to dial, e.g. +15551234567")
    greeting: str | None = Field(
        default=None,
        description=(
            "Opening line the agent speaks when the call is answered. "
            "Defaults to the standard outbound greeting in persona.py."
        ),
    )
    from_number: str | None = Field(
        default=None,
        description="Caller-ID override. Defaults to TELNYX_PHONE_NUMBER.",
    )
    row: int | None = Field(
        default=None,
        description=(
            "Sheet row number for the call-list feedback loop. When set, the "
            "row is encoded into Telnyx client_state so the hangup webhook can "
            "write the outcome back to the right row."
        ),
    )
    sheet_id: str | None = Field(
        default=None,
        description=(
            "Spreadsheet id the row belongs to. Optional but recommended when "
            "more than one sheet feeds the dialer."
        ),
    )


class OutboundCallResponse(BaseModel):
    ok: bool
    call_control_id: str | None
    to: str


def _encode_client_state(payload: dict) -> str:
    """Telnyx requires client_state to be base64-encoded. Kept small to
    stay well under Telnyx's ~8KB limit; we only carry routing metadata."""
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode("ascii")


@router.post("/outbound", response_model=OutboundCallResponse)
async def initiate_outbound_call(req: OutboundCallRequest) -> OutboundCallResponse:
    """Initiate an outbound call.

    Telnyx dials `to`, and when the party answers, the Telnyx Media
    WebSocket fires — the same ConversationOrchestrator that handles
    inbound calls picks it up and drives the conversation.
    """
    media_ws_url = settings.media_ws_url
    if not media_ws_url:
        raise HTTPException(
            status_code=503,
            detail=(
                "APP_BASE_URL is not set — cannot generate the media WebSocket URL. "
                "Add APP_BASE_URL to your .env file."
            ),
        )

    # Carry sheet-row identity through Telnyx so the hangup webhook can write
    # the outcome back to the right row without needing any local mapping that
    # would be lost on restart.
    client_state: str | None = None
    if req.row is not None:
        payload: dict = {"row": req.row}
        if req.sheet_id:
            payload["sheet_id"] = req.sheet_id
        client_state = _encode_client_state(payload)

    try:
        result = await telnyx_service.dial(
            to=req.to,
            from_=req.from_number,
            stream_url=media_ws_url,
            stream_track="inbound_track",
            bidirectional=True,
            client_state=client_state,
        )
    except TelnyxError as exc:
        logger.error("Failed to initiate outbound call to %s: %s", req.to, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Telnyx wraps the response in { "data": { "call_control_id": "..." } }
    data = result.get("data") or result
    call_control_id: str | None = (
        data.get("call_control_id") or data.get("callControlId")
    )

    if call_control_id:
        # Register so the media WS handler knows this is an outbound call and
        # which greeting to use.
        state.outbound_calls[call_control_id] = {
            "greeting": req.greeting,
            "to": req.to,
        }
        logger.info(
            "Outbound call initiated: call=%s to=%s row=%s greeting=%r",
            call_control_id,
            req.to,
            req.row,
            req.greeting,
        )
    else:
        logger.warning(
            "Outbound call created but no call_control_id in response: %s", result
        )

    return OutboundCallResponse(ok=True, call_control_id=call_control_id, to=req.to)
