from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException, status

from .activitypub_handlers import dispatch_activitypub_event
from .activitypub_models import BridgeGatewayEvent
from .runtime import Runtime

logger = logging.getLogger(__name__)


def create_http_app(runtime: Runtime) -> FastAPI:
    # The internal API is intentionally small: one healthcheck plus one trusted
    # event-ingest endpoint from the Fedify gateway.
    app = FastAPI(title="discord-lemmy-bridge-internal-api")

    @app.get("/healthz")
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/activitypub/events")
    async def receive_activitypub_event(
        event: BridgeGatewayEvent,
        authorization: str | None = Header(default=None),
        x_bridge_delivery_id: str | None = Header(default=None),
    ) -> dict[str, str]:
        # Authenticate and deduplicate before touching Discord so gateway
        # retries remain safe.
        _validate_internal_auth(runtime, authorization)
        _validate_delivery_header(x_bridge_delivery_id, event.delivery_id)

        duplicate_response = _begin_event_processing(runtime, event)
        if duplicate_response is not None:
            return duplicate_response

        try:
            result = await dispatch_activitypub_event(event, runtime)
        except Exception as exc:
            _mark_event_failed(runtime, event.delivery_id, str(exc))
            logger.exception("ActivityPub event handling failed for delivery %s", event.delivery_id)
            raise

        _finish_event_processing(runtime, event.delivery_id, result.status, result.detail)
        return {"status": result.status, "detail": result.detail}

    return app


def _validate_internal_auth(runtime: Runtime, authorization: str | None) -> None:
    expected = f"Bearer {runtime.settings.fedify_shared_secret}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid internal authorization")


def _validate_delivery_header(x_bridge_delivery_id: str | None, delivery_id: str) -> None:
    if x_bridge_delivery_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing X-Bridge-Delivery-Id header")
    if x_bridge_delivery_id != delivery_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Delivery ID header does not match payload")


def _begin_event_processing(
    runtime: Runtime, event: BridgeGatewayEvent
) -> dict[str, str] | None:
    # Receipt state is the source of truth for idempotency across duplicate and
    # retry deliveries from the gateway.
    existing = runtime.database.get_event_receipt(event.delivery_id)
    if existing is None:
        runtime.database.create_event_receipt(
            delivery_id=event.delivery_id,
            event_type=event.event_type,
            object_ap_id=_event_object_id(event),
            status="in_progress",
        )
        return None

    if existing.status == "in_progress":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Delivery is already in progress")
    if existing.status in {"processed", "skipped"}:
        return {"status": "duplicate", "detail": existing.detail or existing.status}

    runtime.database.update_event_receipt(
        delivery_id=event.delivery_id,
        status="in_progress",
        detail="retrying failed delivery",
    )
    return None


def _event_object_id(event: BridgeGatewayEvent) -> str:
    # Receipt tracking needs one stable object identifier even though follow
    # lifecycle events do not carry post/comment objects.
    if event.event_type == "follow.accepted":
        return event.object.follow_activity_id
    return event.object.ap_id


def _finish_event_processing(runtime: Runtime, delivery_id: str, status_value: str, detail: str) -> None:
    runtime.database.update_event_receipt(
        delivery_id=delivery_id,
        status=status_value,
        detail=detail,
    )


def _mark_event_failed(runtime: Runtime, delivery_id: str, detail: str) -> None:
    runtime.database.update_event_receipt(
        delivery_id=delivery_id,
        status="failed",
        detail=detail,
    )
