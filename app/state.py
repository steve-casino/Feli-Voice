"""Shared in-process state for cross-handler coordination.

All data here is ephemeral — it lives in the running process and is lost on
restart. We use it only for short-lived call metadata that needs to flow from
a REST handler (which initiates the call) to the WebSocket handler (which
manages the media stream).
"""

from __future__ import annotations

# Maps call_control_id -> outbound call metadata for calls we initiated.
# Populated by POST /calls/outbound, consumed (and removed) by the media WS
# when the call is answered and the stream opens.
#
# Shape: { call_control_id: { "greeting": str | None, "to": str } }
outbound_calls: dict[str, dict] = {}
