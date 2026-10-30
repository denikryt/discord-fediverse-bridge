"""Authenticated private Fluxer Gateway DTO ingress; no native SDK remapping.

Node owns SDK-payload normalization. Python only validates closed neutral DTO
wire shapes and binds the event to an exact configured connector/sink.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from datetime import datetime
from typing import Mapping

from fastapi import APIRouter, Header, HTTPException, Request

from .fluxer_ingress import FluxerInboundHandler
from .internal_auth import validate_internal_bearer
from .platform_types import (
    MessageCreated, MessageDeleted, MessageUpdated, PlatformInboundEvent,
    ThreadCreated, ThreadDeleted, ThreadUpdated,
)

_EVENT_TYPES = {
    cls.__name__: cls for cls in (
        ThreadCreated, ThreadUpdated, ThreadDeleted,
        MessageCreated, MessageUpdated, MessageDeleted,
    )
}
_MAX_EVENT_BYTES = 262_144


def _hydrate(typ: object, value: object) -> object:
    """Strictly parse an SDK-independent dataclass wire tree without coercion."""
    origin = typing.get_origin(typ)
    args = typing.get_args(typ)
    if origin in (typing.Union, types.UnionType):
        for candidate in args:
            try:
                return _hydrate(candidate, value)
            except (TypeError, ValueError):
                continue
        raise ValueError("Invalid event wire field variant")
    if typ is type(None):
        if value is not None:
            raise ValueError("Event wire field expected null")
        return None
    if origin is typing.Literal:
        if value not in args or type(value) is not type(args[0]):
            raise ValueError("Invalid event wire enum")
        return value
    if origin is tuple:
        if not isinstance(value, list) or len(args) != 2 or args[1] is not Ellipsis:
            raise ValueError("Event wire attachments must be an array")
        return tuple(_hydrate(args[0], item) for item in value)
    if typ is datetime:
        if not isinstance(value, str):
            raise ValueError("Event wire timestamp must be ISO8601 text")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Invalid event wire timestamp") from error
    if typ in (str, bool, int):
        if type(value) is not typ:
            raise ValueError("Event wire primitive has wrong type")
        return value
    if isinstance(typ, type) and dataclasses.is_dataclass(typ):
        if not isinstance(value, dict):
            raise ValueError("Event wire reference must be an object")
        fields = {field.name: field for field in dataclasses.fields(typ)}
        if set(value) - fields.keys():
            raise ValueError("Unknown event wire field")
        annotations = typing.get_type_hints(typ)
        kwargs = {}
        for name, field in fields.items():
            if name in value:
                kwargs[name] = _hydrate(annotations[name], value[name])
            elif field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
                raise ValueError("Missing required event wire field")
        return typ(**kwargs)
    raise ValueError("Unsupported event wire field type")


def decode_fluxer_event_wire(body: object) -> PlatformInboundEvent:
    """Construct one checked neutral DTO with no guessed SDK parent/author IDs."""
    if not isinstance(body, dict) or type(body.get("type")) is not str:
        raise ValueError("Expected a typed Fluxer event object")
    cls = _EVENT_TYPES.get(body["type"])
    if cls is None:
        raise ValueError("Unsupported Fluxer event type")
    return _hydrate(cls, {key: val for key, val in body.items() if key != "type"})


def _unique_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys so auth and DTO admission see one meaning."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def create_internal_fluxer_router(
    configured: Mapping[str, tuple[FluxerInboundHandler, str]],
) -> APIRouter:
    """Bind exact connector credentials to one runtime-owned Fluxer sink each."""
    router = APIRouter()

    @router.post("/internal/fluxer/events")
    async def fluxer_event(
        request: Request,
        authorization: str | None = Header(default=None),
        x_fluxer_connector: str | None = Header(default=None),
    ) -> dict[str, bool]:
        """Authenticate first; bound/parse DTO; then ACK the admitted sink call."""
        entry = configured.get(x_fluxer_connector or "")
        if entry is None:
            raise HTTPException(status_code=401, detail="Invalid internal authorization")
        handler, secret = entry
        validate_internal_bearer(authorization=authorization, shared_secret=secret)
        if not handler.accepting:
            raise HTTPException(status_code=503, detail="Fluxer ingress is quiescing")
        # Never trust Content-Length alone; stream in bounded memory, before
        # entering repository-backed sink processing.
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > _MAX_EVENT_BYTES:
                raise HTTPException(status_code=413, detail="Fluxer event too large")
            raw.extend(chunk)
        try:
            body = json.loads(raw, object_pairs_hook=_unique_object_pairs)
            event = decode_fluxer_event_wire(body)
            if event.connector_key != x_fluxer_connector:
                raise ValueError("Wrong connector")
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(status_code=422, detail="Invalid Fluxer event DTO") from None
        try:
            await handler.accept_event(event)
        except RuntimeError as error:
            if str(error) == "Fluxer ingress is quiescing":
                raise HTTPException(status_code=503, detail="Fluxer ingress is quiescing") from None
            raise
        # The ACK does not assert canonical ActivityPub publication or replay.
        return {"accepted": True}

    return router
