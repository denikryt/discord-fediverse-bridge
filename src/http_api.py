"""FastAPI route layer for registration pages and private gateway event intake."""

from __future__ import annotations

import logging
from datetime import timedelta
from html import escape
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from .activitypub_handlers import dispatch_activitypub_event
from .inbound_activity_outcomes import InboundActivityOutcome
from .dashboard import (
    WEB_DIR,
    build_dashboard_payload,
    render_dashboard_html,
)
from .activitypub_models import ActivityPubEvent, BridgeGatewayEvent
from .registration_service import (
    RegistrationError,
    generate_oauth_state,
    generate_session_token,
)
from .runtime import Runtime
from .models import RegistrationSession, utcnow
from .project_version import APP_VERSION
from .internal_auth import validate_internal_bearer
from .internal_fedify_api import create_internal_fedify_router

logger = logging.getLogger(__name__)
REGISTRATION_HTML_PATH = WEB_DIR / "registration.html"


def create_http_app(runtime: Runtime) -> FastAPI:
    """Create the FastAPI app for internal bridge events and public registration."""

    # The app exposes both the trusted gateway ingest endpoint and the Stage 5
    # public registration pages. The internal auth rules apply only to the
    # private event-ingest surface.
    app = FastAPI(title="discord-lemmy-bridge-internal-api")
    app.include_router(create_internal_fedify_router(runtime))
    # Dashboard assets stay under one namespace so nginx can proxy the whole
    # prefix to Python instead of learning every file name individually.
    app.mount(
        "/dashboard/static",
        StaticFiles(directory=str(WEB_DIR)),
        name="dashboard-static",
    )

    @app.get("/media/profile/{public_id}")
    async def profile_media(public_id: str) -> Response:
        """Serve only persisted immutable profile media through opaque IDs."""
        import re

        if re.fullmatch(r"[A-Za-z0-9_-]{20,64}", public_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        asset = runtime.database.profile_media.get_by_public_id(public_id)
        if asset is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        try:
            path = runtime.profile_media_store.resolve_storage_key(asset.storage_key)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
        return FileResponse(
            path,
            media_type=asset.content_type,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(asset.byte_size),
            },
        )

    @app.get("/healthz")
    async def healthcheck() -> dict[str, str]:
        """Return a minimal healthcheck for process supervision."""
        return {"status": "ok", "version": APP_VERSION}

    @app.get("/register")
    async def register_page(request: Request) -> Response:
        """Render the current registration step for the browser session."""
        session = _load_or_create_registration_session(runtime, request)
        existing_user = _existing_registration_for_session(runtime, session)

        if existing_user is not None:
            response = _html_response(
                title="Already registered",
                heading="Already registered",
                subtitle="This Discord account already owns one local ActivityPub identity.",
                body=(
                    f"<p>Your ActivityPub identity already exists as <strong>{_actor_handle(runtime, existing_user.activitypub_username)}</strong>.</p>"
                    f'<p><a href="{existing_user.actor_url}">{existing_user.actor_url}</a></p>'
                ),
            )
            _apply_session_cookie(runtime, response, session.session_token)
            return response

        if session.discord_user_id is not None:
            response = _html_response(
                title="Choose username",
                heading="Choose your ActivityPub username",
                subtitle="Finish registration by choosing the public username that this bridge will expose to the Fediverse.",
                body=(
                    f"<p>Signed in as Discord user <strong>{session.discord_username or session.discord_user_id}</strong>.</p>"
                    '<form class="registration-form" method="post" action="/register/complete">'
                    '<label for="username">Username</label>'
                    '<p class="muted registration-form-help">Use a stable lowercase name you want to keep as your local actor handle.</p>'
                    '<input id="username" name="username" type="text" required />'
                    '<button class="registration-submit" type="submit">Create identity</button>'
                    "</form>"
                ),
            )
            _apply_session_cookie(runtime, response, session.session_token)
            return response

        response = _html_response(
            title="Register",
            heading="Register your ActivityPub identity",
            subtitle="Use Discord OAuth to verify account ownership before choosing a local ActivityPub username.",
            body=(
                "<p>This flow creates one local Fediverse identity linked to your Discord account.</p>"
                '<div class="registration-actions"><a class="button-link" href="/auth/discord/start">Continue with Discord</a></div>'
            ),
        )
        _apply_session_cookie(runtime, response, session.session_token)
        return response

    @app.get("/auth/discord/start")
    async def start_discord_auth(request: Request) -> RedirectResponse:
        """Start the Discord OAuth redirect flow for one browser session."""
        session = _load_or_create_registration_session(runtime, request)
        oauth_state = generate_oauth_state()
        runtime.database.registration_sessions.update_registration_session_oauth_state(
            session_token=session.session_token,
            oauth_state=oauth_state,
            expires_at=_registration_session_expiry(runtime),
        )
        redirect = RedirectResponse(
            runtime.discord_oauth_client.build_authorization_url(oauth_state),
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        )
        # The session cookie must survive the external Discord redirect so the
        # callback can resume the correct browser-owned registration flow.
        _apply_session_cookie(runtime, redirect, session.session_token)
        return redirect

    @app.get("/auth/discord/callback")
    async def discord_auth_callback(
        request: Request,
        code: str,
        state: str,
    ) -> RedirectResponse:
        """Handle the Discord OAuth callback and persist the authenticated user."""
        session = _require_registration_session(runtime, request)
        if session.oauth_state != state:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid OAuth state",
            )

        access_token = (
            await runtime.discord_oauth_client.exchange_code_for_access_token(code)
        )
        profile = await runtime.discord_oauth_client.fetch_user_profile(access_token)
        runtime.database.registration_sessions.update_registration_session_discord_identity(
            session_token=session.session_token,
            discord_user_id=profile.user_id,
            discord_username=profile.username,
            discord_avatar_url=profile.avatar_url,
            expires_at=_registration_session_expiry(runtime),
        )
        redirect = RedirectResponse(
            "/register",
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        )
        _apply_session_cookie(runtime, redirect, session.session_token)
        return redirect

    @app.post("/register/complete")
    async def complete_registration(request: Request) -> Response:
        """Validate the chosen username and create the shared user record."""
        session = _require_registration_session(runtime, request)
        if session.discord_user_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Discord authentication is required before registration completes",
            )

        username = _extract_form_field(await request.body(), "username")
        try:
            outcome, actor = runtime.registration_service.create_or_get_user(
                discord_user_id=session.discord_user_id,
                requested_username=username,
            )
        except RegistrationError as exc:
            return _html_response(
                title="Registration error",
                heading="Registration error",
                subtitle="The bridge could not complete this registration request.",
                body=f"<p>{exc}</p>",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if outcome == "existing":
            return _html_response(
                title="Already registered",
                heading="Already registered",
                subtitle="This Discord account already owns one local ActivityPub identity.",
                body=(
                    f"<p>Your ActivityPub identity already exists as <strong>{_actor_handle(runtime, actor.activitypub_username)}</strong>.</p>"
                    f'<p><a href="{actor.actor_url}">{actor.actor_url}</a></p>'
                ),
            )

        runtime.database.registration_sessions.mark_registration_session_completed(
            session_token=session.session_token,
            activitypub_username=actor.activitypub_username,
            expires_at=_registration_session_expiry(runtime),
        )
        redirect = RedirectResponse(
            "/register/success",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        _apply_session_cookie(runtime, redirect, session.session_token)
        return redirect

    @app.get("/register/success")
    async def registration_success(request: Request) -> Response:
        """Render the final success page for a completed registration session."""
        session = _require_registration_session(runtime, request)
        if session.activitypub_username is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Registration has not completed yet",
            )
        user = runtime.database.users.get_user_by_activitypub_username(
            session.activitypub_username
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Registration session does not point to an existing actor",
            )
        return _html_response(
            title="Registration complete",
            heading="Registration complete",
            subtitle="Your local ActivityPub identity is ready to use.",
            body=(
                f"<p>Your ActivityPub handle is <strong>{_actor_handle(runtime, user.activitypub_username)}</strong>.</p>"
                f'<p>Your actor URL is <a href="{user.actor_url}">{user.actor_url}</a>.</p>'
            ),
        )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_page() -> HTMLResponse:
        """Render the public dashboard browser shell on the root URL."""
        return HTMLResponse(
            render_dashboard_html(page_title=runtime.settings.dashboard_page_title)
        )

    @app.get("/dashboard")
    async def dashboard_redirect() -> RedirectResponse:
        """Redirect legacy dashboard links to the canonical root page."""
        return RedirectResponse("/", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    @app.get("/dashboard/data")
    async def dashboard_data() -> dict[str, object]:
        """Return safe public dashboard metadata as JSON."""
        return build_dashboard_payload(runtime)

    @app.post("/internal/activitypub/events")
    async def receive_activitypub_event(
        event: BridgeGatewayEvent,
        authorization: str | None = Header(default=None),
        x_bridge_delivery_id: str | None = Header(default=None),
    ) -> dict[str, str | None]:
        # Authenticate and deduplicate before touching Discord so gateway
        # retries remain safe.
        validate_internal_bearer(
            authorization=authorization,
            shared_secret=runtime.settings.fedify_shared_secret,
        )
        _validate_delivery_header(x_bridge_delivery_id, event.delivery_id)
        # The gateway selected this recipient only after verifying the signed
        # activity and inbox route. Python independently verifies that the
        # scoped identity is one of our local Group actors before any receipt.
        _validate_verified_local_group_destination(runtime, event)

        # NodeBB roots and replies both use /post/{pid}; resolve a signed
        # Delete's kind from the verified Group's canonical DB identity before
        # receipt admission. The wire delivery ID/header never changes.
        if getattr(event, "event_type", None) in {"post.deleted", "comment.deleted"}:
            from .community_sync.edit_delete import canonicalize_remote_delete_kind
            event = canonicalize_remote_delete_kind(runtime.database, event)

        # Only a durable failed/deferred receipt establishes that this
        # Follow ID was observed before. Keep the internal provenance before
        # resetting the receipt for retry; never trust an external flag.
        duplicate_response, is_retry = _begin_event_attempt(runtime, event)
        if duplicate_response is not None:
            return duplicate_response

        try:
            result = await dispatch_activitypub_event(event, runtime, is_retry=is_retry)
        except Exception as exc:
            _mark_event_failed(runtime, event.delivery_id, str(exc),
                               verified_destination_group_actor_id=_receipt_group_scope(event))
            logger.exception(
                "ActivityPub event handling failed for delivery %s", event.delivery_id
            )
            raise

        _finish_event_processing(
            runtime, event.delivery_id, result.status, result.outcome, result.detail,
            verified_destination_group_actor_id=_receipt_group_scope(event),
        )
        return {
            "status": result.status,
            "outcome": result.outcome.value,
            "detail": result.detail,
        }

    return app


def _load_or_create_registration_session(
    runtime: Runtime, request: Request
) -> RegistrationSession:
    """Load the current session cookie or create a fresh registration session."""
    session_cookie_name = runtime.settings.registration_session_cookie_name
    session_token = request.cookies.get(session_cookie_name)
    session = runtime.database.registration_sessions.get_registration_session_by_token(
        session_token
    )
    if session is None or _registration_session_is_expired(session):
        session_token = generate_session_token()
        session = runtime.database.registration_sessions.create_registration_session(
            session_token=session_token,
            expires_at=_registration_session_expiry(runtime),
        )

    return session


def _require_registration_session(
    runtime: Runtime, request: Request
) -> RegistrationSession:
    """Load the active registration session or reject the request."""
    session = runtime.database.registration_sessions.get_registration_session_by_token(
        request.cookies.get(runtime.settings.registration_session_cookie_name)
    )
    if session is None or _registration_session_is_expired(session):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing or expired registration session",
        )
    return session


def _registration_session_expiry(runtime: Runtime):
    """Compute the expiry timestamp for a freshly touched registration session."""
    return utcnow() + timedelta(
        seconds=runtime.settings.registration_session_ttl_seconds
    )


def _registration_session_is_expired(session: RegistrationSession) -> bool:
    """Compare one persisted expiry timestamp against current UTC safely."""
    # SQLite may hand timezone columns back as naive datetimes, so the
    # registration flow treats naive expiries as UTC instead of crashing.
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=utcnow().tzinfo)
    return expires_at <= utcnow()


def _apply_session_cookie(
    runtime: Runtime, response: Response, session_token: str
) -> None:
    """Write the browser session cookie used by the registration flow."""
    response.set_cookie(
        key=runtime.settings.registration_session_cookie_name,
        value=session_token,
        max_age=runtime.settings.registration_session_ttl_seconds,
        httponly=True,
        samesite="lax",
    )


def _extract_form_field(raw_body: bytes, field_name: str) -> str:
    """Parse one URL-encoded form field without adding a form dependency."""
    # Stage 5 serves plain HTML without a template/form stack, so parsing the
    # single username field directly keeps the backend dependency surface small.
    parsed = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
    values = parsed.get(field_name)
    if not values:
        return ""
    return values[0]


def _existing_registration_for_session(runtime: Runtime, session: RegistrationSession):
    """Return the already-created actor for the current Discord identity, if any."""
    if session.discord_user_id is None:
        return None
    return runtime.database.users.get_user_by_discord_user_id(session.discord_user_id)


def _actor_handle(runtime: Runtime, username: str) -> str:
    """Render the local `@user@domain` handle for human-facing pages."""
    return runtime.registration_service.actor_handle(username)


def _html_response(
    *,
    title: str,
    heading: str,
    subtitle: str,
    body: str,
    status_code: int = 200,
) -> HTMLResponse:
    """Render one styled registration page from the shared HTML shell."""
    template = REGISTRATION_HTML_PATH.read_text(encoding="utf-8")
    rendered = (
        template.replace("__REGISTRATION_TITLE__", escape(title, quote=True))
        .replace("__REGISTRATION_HEADING__", escape(heading))
        .replace("__REGISTRATION_SUBTITLE__", escape(subtitle))
        .replace("__REGISTRATION_BODY__", body)
        .replace(
            "__REGISTRATION_ICON_VERSION__",
            _registration_asset_cache_token(WEB_DIR / "icon.png"),
        )
        .replace(
            "__REGISTRATION_CSS_VERSION__",
            _registration_asset_cache_token(WEB_DIR / "dashboard.css"),
        )
    )
    return HTMLResponse(
        rendered,
        status_code=status_code,
    )


def _registration_asset_cache_token(path) -> str:
    """Return one cache-busting token for registration-page static assets."""
    return str(path.stat().st_mtime_ns)


def _validate_delivery_header(
    x_bridge_delivery_id: str | None, delivery_id: str
) -> None:
    if x_bridge_delivery_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Bridge-Delivery-Id header",
        )
    if x_bridge_delivery_id != delivery_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Delivery ID header does not match payload",
        )


def _receipt_group_scope(event: BridgeGatewayEvent) -> str | None:
    """Only verified local content owns a scoped receipt; all else is global."""
    if isinstance(event, ActivityPubEvent):
        return event.verified_destination_group_actor_id
    return None


def _validate_verified_local_group_destination(
    runtime: Runtime, event: BridgeGatewayEvent,
) -> None:
    """Reject local-Group content lacking a trusted gateway-selected tenant.

    Remote NodeBB/Lemmy Group distributions, ordinary Follow and personal feed
    traffic retain their independent ingress/receipt domains. Local groups
    cannot be chosen from an unverified first ``to``/``cc`` candidate.
    """
    if not isinstance(event, ActivityPubEvent):
        return
    scope = event.verified_destination_group_actor_id
    candidate = event.community_actor_id
    local_target = (runtime.database.local_communities.get_local_community_by_actor_url(candidate)
                    if candidate else None)
    if scope is None:
        if local_target is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Local Group content requires verified destination")
        return
    if scope != candidate or local_target is None or local_target.actor_url != scope:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Unverified local Group destination")


def _begin_event_processing(
    runtime: Runtime, event: BridgeGatewayEvent
) -> dict[str, str | None] | None:
    """Keep the public receipt helper's response contract for existing callers."""
    result, _is_retry = _begin_event_attempt(runtime, event)
    return result


def _begin_event_attempt(
    runtime: Runtime, event: BridgeGatewayEvent
) -> tuple[dict[str, str | None] | None, bool]:
    """Claim a receipt and capture Follow retry provenance before resetting it.

    The retry flag is internal-only and meaningful solely for community
    Follow requests. A failed receipt may outlive a successful Undo, so the
    subscriber repository must still decide if this generation is current.
    """
    # Receipt state is the source of truth for idempotency across duplicate and
    # retry deliveries from the gateway.
    scope = _receipt_group_scope(event)
    existing = runtime.database.event_receipts.get_event_receipt(
        event.delivery_id, verified_destination_group_actor_id=scope)
    if existing is None:
        try:
            runtime.database.event_receipts.create_event_receipt(
                delivery_id=event.delivery_id,
                verified_destination_group_actor_id=scope,
                event_type=event.event_type,
                object_ap_id=_event_object_id(event),
                status="in_progress",
            )
            return None, False
        except IntegrityError:
            # Another handler may have reserved the same wire ID and Group
            # between our read and INSERT. Do not convert that valid duplicate
            # race into HTTP 500 or borrow another tenant's receipt.
            existing = runtime.database.event_receipts.get_event_receipt(
                event.delivery_id, verified_destination_group_actor_id=scope)
            if existing is None:
                raise

    # A wire ID reused for a different object within the same verified Group
    # cannot borrow an earlier terminal result or mutate that object's receipt.
    if scope is not None and (
        existing.event_type != event.event_type
        or existing.object_ap_id != _event_object_id(event)
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Delivery identity conflicts within receipt scope")
    if existing.status == "in_progress":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Delivery is already in progress",
        )
    if existing.status in {"processed", "skipped"}:
        return {
            "status": "duplicate",
            "outcome": existing.outcome,
            "detail": existing.detail or existing.status,
        }, False
    is_retry = event.event_type == "local.follow_requested" and (
        existing.event_type == event.event_type
        and existing.object_ap_id == _event_object_id(event)
    )
    # If the same wire ID once carried a different event/object, it cannot
    # safely prove a new Follow first-seen or legitimate retry generation.
    if event.event_type == "local.follow_requested" and not is_retry:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Follow delivery ID conflicts with a prior event",
        )
    if existing.status not in {"failed", "deferred"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Unsupported receipt state for Follow retry",
        )
    if existing.status == "deferred":
        runtime.database.event_receipts.update_event_receipt(
            delivery_id=event.delivery_id,
            verified_destination_group_actor_id=scope,
            status="in_progress",
            outcome=None,
            detail="retrying deferred delivery",
        )
        return None, is_retry

    runtime.database.event_receipts.update_event_receipt(
        delivery_id=event.delivery_id,
        verified_destination_group_actor_id=scope,
        status="in_progress",
        outcome=None,
        detail="retrying failed delivery",
    )
    return None, is_retry


def _event_object_id(event: BridgeGatewayEvent) -> str:
    # Receipt tracking needs one stable object identifier even though follow
    # lifecycle events do not carry post/comment objects.
    if event.event_type in {"follow.accepted", "local.follow_requested"}:
        return event.object.follow_activity_id
    if event.event_type == "local.unfollow_requested":
        return event.object.follow_activity_id or event.delivery_id
    return event.object.ap_id


def _finish_event_processing(
    runtime: Runtime,
    delivery_id: str,
    status_value: str,
    outcome: InboundActivityOutcome,
    detail: str,
    *, verified_destination_group_actor_id: str | None = None,
) -> None:
    """Persist one terminal result only inside the verified receipt scope."""
    runtime.database.event_receipts.update_event_receipt(
        delivery_id=delivery_id,
        verified_destination_group_actor_id=verified_destination_group_actor_id,
        status=status_value,
        outcome=outcome,
        detail=detail,
    )


def _mark_event_failed(
    runtime: Runtime, delivery_id: str, detail: str, *,
    verified_destination_group_actor_id: str | None = None,
) -> None:
    """Persist one failed attempt without affecting another Group's receipt."""
    runtime.database.event_receipts.update_event_receipt(
        delivery_id=delivery_id,
        verified_destination_group_actor_id=verified_destination_group_actor_id,
        status="failed",
        outcome=InboundActivityOutcome.PROCESSING_FAILED,
        detail=detail,
    )
