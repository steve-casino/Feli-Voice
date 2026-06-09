"""Telnyx Call Control + webhook-signature helpers.

We use Telnyx's REST Call Control API to answer calls, hang up, and start
bidirectional Media Streaming. We use the `telnyx` Python SDK only for
webhook signature verification (it's a sync SDK, so we keep it off the hot
path).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BASE = "https://api.telnyx.com/v2"


class TelnyxError(RuntimeError):
    """Raised when Telnyx returns a non-2xx response."""


class TelnyxService:
    """Thin async client over Telnyx Call Control REST API."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.telnyx_api_key
        self._client = httpx.AsyncClient(
            base_url=_BASE,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post(path, json=body)
        if resp.status_code >= 400:
            logger.error(
                "Telnyx API error: %s %s -> %s %s",
                "POST",
                path,
                resp.status_code,
                resp.text,
            )
            raise TelnyxError(f"{resp.status_code}: {resp.text}")
        return resp.json()

    # ---- Call Control commands --------------------------------------------

    async def answer(
        self,
        call_control_id: str,
        *,
        stream_url: str | None = None,
        stream_track: str = "inbound_track",
        bidirectional: bool = True,
    ) -> dict[str, Any]:
        """Answer an incoming call.

        If `stream_url` is provided, Telnyx will also start media streaming
        as part of the answer. In bidirectional mode the same WebSocket is
        used to push TTS audio back into the call.
        """
        body: dict[str, Any] = {}
        if stream_url:
            body.update(
                {
                    "stream_url": stream_url,
                    "stream_track": stream_track,
                }
            )
            if bidirectional:
                body.update(
                    {
                        # Telnyx's bidirectional mode params. Codec must match
                        # what we tell ElevenLabs to produce (PCMU = μ-law).
                        "stream_bidirectional_mode": "rtp",
                        "stream_bidirectional_codec": "PCMU",
                    }
                )
        return await self._post(f"/calls/{call_control_id}/actions/answer", body)

    async def hangup(self, call_control_id: str) -> dict[str, Any]:
        return await self._post(f"/calls/{call_control_id}/actions/hangup", {})

    async def dial(
        self,
        to: str,
        from_: str | None = None,
        *,
        connection_id: str | None = None,
        stream_url: str | None = None,
        stream_track: str = "both_tracks",
        bidirectional: bool = True,
        timeout_secs: int = 30,
        client_state: str | None = None,
    ) -> dict[str, Any]:
        """Initiate an outbound call.

        Requires TELNYX_CONNECTION_ID and TELNYX_PHONE_NUMBER in config (or
        pass connection_id / from_ explicitly).

        If `stream_url` is provided, Telnyx will automatically start media
        streaming as soon as the called party answers — no need to call
        streaming_start separately.
        """
        conn = connection_id or settings.telnyx_connection_id
        if not conn:
            raise TelnyxError(
                "TELNYX_CONNECTION_ID is required for outbound calls. "
                "Add it to your .env file."
            )
        caller = from_ or settings.telnyx_phone_number
        if not caller:
            raise TelnyxError(
                "TELNYX_PHONE_NUMBER is required for outbound calls (or pass "
                "from_ explicitly)."
            )

        body: dict[str, Any] = {
            "to": to,
            "from": caller,
            "connection_id": conn,
            "timeout_secs": timeout_secs,
        }
        if client_state:
            body["client_state"] = client_state
        if stream_url:
            body["stream_url"] = stream_url
            body["stream_track"] = stream_track
            if bidirectional:
                body["stream_bidirectional_mode"] = "rtp"
                body["stream_bidirectional_codec"] = "PCMU"
        return await self._post("/calls", body)

    async def streaming_start(
        self,
        call_control_id: str,
        stream_url: str,
        *,
        stream_track: str = "both_tracks",
        bidirectional: bool = True,
    ) -> dict[str, Any]:
        """Explicitly start media streaming on an already-answered call.

        Usually not needed for outbound calls when you pass stream_url to
        dial() — Telnyx starts streaming automatically on answer. Useful as
        a fallback or for calls answered via SIP.
        """
        body: dict[str, Any] = {
            "stream_url": stream_url,
            "stream_track": stream_track,
        }
        if bidirectional:
            body["stream_bidirectional_mode"] = "rtp"
            body["stream_bidirectional_codec"] = "PCMU"
        return await self._post(
            f"/calls/{call_control_id}/actions/streaming_start", body
        )

    async def speak(
        self,
        call_control_id: str,
        text: str,
        *,
        voice: str = "female",
        language: str = "en-US",
    ) -> dict[str, Any]:
        """Play TTS via Telnyx's built-in voice — useful for fallback messages
        before our ElevenLabs pipeline kicks in."""
        return await self._post(
            f"/calls/{call_control_id}/actions/speak",
            {"payload": text, "voice": voice, "language": language},
        )


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------


def verify_webhook_signature(
    raw_body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
    public_key: str | None = None,
) -> bool:
    """Verify a Telnyx webhook signature.

    Returns True if the signature checks out or if no public key is configured
    (dev convenience — we warn loudly in that case). The caller should still
    check the returned bool and 401 if False.
    """
    pk = public_key or settings.telnyx_public_key
    if not pk:
        logger.warning(
            "TELNYX_PUBLIC_KEY not set; skipping webhook signature verification. "
            "Set it in production."
        )
        return True

    if not signature_header or not timestamp_header:
        logger.warning("Missing Telnyx signature headers")
        return False

    try:
        # Local import so the rest of the module works even if telnyx isn't
        # installed (it's a heavy dep we only need here).
        import telnyx  # type: ignore[import]

        telnyx.public_key = pk
        telnyx.Webhook.construct_event(
            raw_body.decode("utf-8"),
            signature_header,
            timestamp_header,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Telnyx webhook signature verification failed: %s", e)
        return False


# Module-level singleton. Imported as `from app.services.telnyx_service import telnyx_service`.
telnyx_service = TelnyxService()
