"""Entry point for the Felicetti Law Firm voice agent FastAPI app."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.config import settings
from app.handlers.dashboard import router as dashboard_router
from app.handlers.outbound_call import router as outbound_call_router
from app.handlers.telnyx_media import router as telnyx_media_router
from app.handlers.telnyx_webhook import router as telnyx_webhook_router

# Configure logging early. uvicorn will respect this for our app loggers.
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


app = FastAPI(
    title="Felicetti Law Firm Voice Agent",
    version="0.1.0",
    description="Voice agent service for the Felicetti Law Firm.",
)


app.include_router(telnyx_webhook_router)
app.include_router(telnyx_media_router)
app.include_router(outbound_call_router)
app.include_router(dashboard_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Lightweight health check endpoint.

    Confirms env is loaded and the app is up. Doesn't make any outbound
    calls, so it's cheap and safe to hit frequently from Cloudflare.
    """
    return {
        "status": "ok",
        "app_base_url": settings.app_base_url or "(not set)",
        "media_ws_url": settings.media_ws_url or "(not set — set APP_BASE_URL)",
    }


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("Felicetti Voice Agent starting up")
    logger.info("  model:        %s", settings.anthropic_model)
    logger.info("  voice:        %s", settings.elevenlabs_voice_id)
    logger.info("  app_base_url: %s", settings.app_base_url or "(not set)")
    logger.info("  media_ws_url: %s", settings.media_ws_url or "(not set)")
    if not settings.app_base_url:
        logger.warning(
            "APP_BASE_URL is not set. Inbound calls will be answered but no "
            "media streaming will start. Set APP_BASE_URL to your Cloudflare "
            "tunnel URL (e.g. https://api.felivoice.com)."
        )
    if not settings.telnyx_public_key:
        logger.warning(
            "TELNYX_PUBLIC_KEY is not set. Webhook signature verification is "
            "disabled. OK for dev, NOT OK for production."
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )
