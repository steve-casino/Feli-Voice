"""Telnyx webhook handler.

Telnyx POSTs every call event to this endpoint. We care primarily about
`call.initiated` (someone called the firm) — we answer the call and tell
Telnyx to start bidirectional media streaming into our `/telnyx/media`
WebSocket.

All other events we log and 200-OK; we can grow the handler later.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import settings
from app.models.telnyx import TelnyxWebhookEnvelope
from app.services.hangup_spool import append_hangup
from app.services.telnyx_service import telnyx_service, verify_webhook_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/telnyx", tags=["telnyx"])


@router.post("/webhook")
async def telnyx_webhook(
    request: Request,
    telnyx_signature_ed25519: str | None = Header(default=None, alias="telnyx-signature-ed25519"),
    telnyx_timestamp: str | None = Header(default=None, alias="telnyx-timestamp"),
) -> dict:
    raw_body = await request.body()

    if not verify_webhook_signature(
        raw_body=raw_body,
        signature_header=telnyx_signature_ed25519,
        timestamp_header=telnyx_timestamp,
    ):
        raise HTTPException(status_code=401, detail="Invalid Telnyx signature")

    try:
        envelope = TelnyxWebhookEnvelope.model_validate_json(raw_body)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not parse Telnyx webhook body: %s", e)
        return {"ok": True}  # ack so Telnyx doesn't retry

    event_type = envelope.data.event_type
    payload = envelope.data.payload
    logger.info(
        "Telnyx event %s call=%s from=%s to=%s",
        event_type,
        payload.call_control_id,
        payload.from_,
        payload.to,
    )

    if event_type == "call.initiated":
        if payload.direction == "incoming":
            # ---- Inbound: answer and start media streaming ----
            media_ws_url = settings.media_ws_url
            if not media_ws_url:
                logger.error(
                    "APP_BASE_URL is not set, so I can't tell Telnyx where to stream. "
                    "Set APP_BASE_URL in .env to your Cloudflare tunnel URL "
                    "(e.g. https://api.felivoice.com)."
                )
                # Still answer so the caller doesn't hear silence.
                await telnyx_service.answer(payload.call_control_id)
                return {"ok": True}

            logger.info(
                "Answering inbound call %s with stream -> %s",
                payload.call_control_id,
                media_ws_url,
            )
            await telnyx_service.answer(
                payload.call_control_id,
                stream_url=media_ws_url,
                stream_track="inbound_track",
                bidirectional=True,
            )

        elif payload.direction == "outgoing":
            # ---- Outbound: we initiated this call via POST /calls/outbound ----
            # Streaming was already configured in the dial() call. Nothing to do
            # here except log; the media WS will connect when they answer.
            logger.info(
                "Outbound call initiated: call=%s to=%s",
                payload.call_control_id,
                payload.to,
            )

    elif event_type == "call.answered":
        if payload.direction == "outgoing":
            # The called party picked up. Streaming should start automatically
            # because we passed stream_url in the dial() request. Log it and
            # let the media WS handler take over.
            logger.info(
                "Outbound call answered: call=%s to=%s — media stream should be connecting",
                payload.call_control_id,
                payload.to,
            )
        else:
            logger.info(
                "Inbound call answered: call=%s", payload.call_control_id
            )

    elif event_type == "call.hangup":
        # Feedback loop for the outbound dialer. We round-tripped the sheet row
        # through Telnyx via `client_state` at dial time; spool it now and let
        # tools/dialer.py flush to the sheet on its next run.
        if payload.direction == "outgoing":
            logger.info(
                "Outbound call.hangup: call=%s cause=%s source=%s",
                payload.call_control_id,
                payload.hangup_cause,
                payload.hangup_source,
            )
            await append_hangup(
                spool_path=settings.hangup_spool_path,
                call_control_id=payload.call_control_id,
                client_state=payload.client_state,
                hangup_cause=payload.hangup_cause,
                hangup_source=payload.hangup_source,
                to=payload.to,
                start_time=payload.start_time,
                end_time=payload.end_time,
            )

    elif event_type in {"streaming.started", "streaming.stopped"}:
        # Just log — the media WS handles conversation teardown.
        pass

    return {"ok": True}
