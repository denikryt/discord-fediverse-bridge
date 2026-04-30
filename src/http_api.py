from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException, status

from .activitypub_handlers import dispatch_activitypub_event
from .activitypub_models import ActivityPubEvent
from .runtime import Runtime

logger = logging.getLogger(__name__)


def create_http_app(runtime: Runtime) -> FastAPI:
    app = FastAPI(title="discord-lemmy-bridge-internal-api")

    @app.get("/healthz")
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/activitypub/events")
    async def receive_activitypub_event(
        event: ActivityPubEvent,
        authorization: str | None = Header(default=None),
        x_bridge_delivery_id: str | None = Header(default=None),
    ) -> dict[str, str]:
        _validate_internal_auth(runtime, authorization)
        if x_bridge_delivery_id is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing X-Bridge-Delivery-Id header")
        if x_bridge_delivery_id != event.delivery_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Delivery ID header does not match payload")

        existing = runtime.database.get_event_receipt(event.delivery_id)
        if existing is not None:
            if existing.status == "in_progress":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Delivery is already in progress")
            if existing.status in {"processed", "skipped"}:
                return {"status": "duplicate", "detail": existing.detail or existing.status}
            runtime.database.update_event_receipt(
                delivery_id=event.delivery_id,
                status="in_progress",
                detail="retrying failed delivery",
            )
        else:
            runtime.database.create_event_receipt(
                delivery_id=event.delivery_id,
                event_type=event.event_type,
                object_ap_id=event.object.ap_id,
                status="in_progress",
            )

        try:
            result = await dispatch_activitypub_event(event, runtime)
        except Exception as exc:
            runtime.database.update_event_receipt(
                delivery_id=event.delivery_id,
                status="failed",
                detail=str(exc),
            )
            logger.exception("ActivityPub event handling failed for delivery %s", event.delivery_id)
            raise

        runtime.database.update_event_receipt(
            delivery_id=event.delivery_id,
            status=result.status,
            detail=result.detail,
        )
        return {"status": result.status, "detail": result.detail}

    return app


def _validate_internal_auth(runtime: Runtime, authorization: str | None) -> None:
    expected = f"Bearer {runtime.settings.fedify_shared_secret}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid internal authorization")
