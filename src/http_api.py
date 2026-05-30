"""FastAPI route layer for registration pages and private gateway event intake."""

from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .activitypub_handlers import dispatch_activitypub_event
from .dashboard import WEB_DIR, build_dashboard_payload, render_dashboard_html
from .activitypub_models import BridgeGatewayEvent
from .registration_service import RegistrationError, generate_oauth_state, generate_session_token
from .runtime import Runtime
from .models import RegistrationSession, utcnow

logger = logging.getLogger(__name__)


def create_http_app(runtime: Runtime) -> FastAPI:
    """Create the FastAPI app for internal bridge events and public registration."""

    # The app exposes both the trusted gateway ingest endpoint and the Stage 5
    # public registration pages. The internal auth rules apply only to the
    # private event-ingest surface.
    app = FastAPI(title="discord-lemmy-bridge-internal-api")
    # Dashboard assets stay under one namespace so nginx can proxy the whole
    # prefix to Python instead of learning every file name individually.
    app.mount(
        "/dashboard/static",
        StaticFiles(directory=str(WEB_DIR)),
        name="dashboard-static",
    )

    @app.get("/healthz")
    async def healthcheck() -> dict[str, str]:
        """Return a minimal healthcheck for process supervision."""
        return {"status": "ok"}

    @app.get("/register")
    async def register_page(request: Request) -> Response:
        """Render the current registration step for the browser session."""
        session = _load_or_create_registration_session(runtime, request)
        existing_user = _existing_registration_for_session(runtime, session)

        if existing_user is not None:
            response = _html_response(
                title="Already registered",
                body=(
                    "<h1>Already registered</h1>"
                    f"<p>Your ActivityPub identity already exists as <strong>{_actor_handle(runtime, existing_user.activitypub_username)}</strong>.</p>"
                    f"<p>Actor URL: <a href=\"{existing_user.actor_url}\">{existing_user.actor_url}</a></p>"
                ),
            )
            _apply_session_cookie(runtime, response, session.session_token)
            return response

        if session.discord_user_id is not None:
            response = _html_response(
                title="Choose username",
                body=(
                    "<h1>Choose your ActivityPub username</h1>"
                    f"<p>Signed in as Discord user <strong>{session.discord_username or session.discord_user_id}</strong>.</p>"
                    "<form method=\"post\" action=\"/register/complete\">"
                    "<label for=\"username\">Username</label>"
                    "<input id=\"username\" name=\"username\" type=\"text\" required />"
                    "<button type=\"submit\">Create identity</button>"
                    "</form>"
                ),
            )
            _apply_session_cookie(runtime, response, session.session_token)
            return response

        response = _html_response(
            title="Register",
            body=(
                "<h1>Register your ActivityPub identity</h1>"
                "<p>Use Discord OAuth to verify ownership of your Discord account before choosing a local ActivityPub username.</p>"
                "<p><a href=\"/auth/discord/start\">Continue with Discord</a></p>"
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

        access_token = await runtime.discord_oauth_client.exchange_code_for_access_token(
            code
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
                body=f"<h1>Registration error</h1><p>{exc}</p>",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if outcome == "existing":
            return _html_response(
                title="Already registered",
                body=(
                    "<h1>Already registered</h1>"
                    f"<p>Your ActivityPub identity already exists as <strong>{_actor_handle(runtime, actor.activitypub_username)}</strong>.</p>"
                    f"<p>Actor URL: <a href=\"{actor.actor_url}\">{actor.actor_url}</a></p>"
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
            body=(
                "<h1>Registration complete</h1>"
                f"<p>Your ActivityPub handle is <strong>{_actor_handle(runtime, user.activitypub_username)}</strong>.</p>"
                f"<p>Actor URL: <a href=\"{user.actor_url}\">{user.actor_url}</a></p>"
            ),
        )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_page() -> HTMLResponse:
        """Render the public dashboard browser shell on the root URL."""
        return HTMLResponse(render_dashboard_html())

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


def _load_or_create_registration_session(
    runtime: Runtime, request: Request
) -> RegistrationSession:
    """Load the current session cookie or create a fresh registration session."""
    session_cookie_name = runtime.settings.registration_session_cookie_name
    session_token = request.cookies.get(session_cookie_name)
    session = runtime.database.registration_sessions.get_registration_session_by_token(session_token)
    if session is None or _registration_session_is_expired(session):
        session_token = generate_session_token()
        session = runtime.database.registration_sessions.create_registration_session(
            session_token=session_token,
            expires_at=_registration_session_expiry(runtime),
        )

    return session


def _require_registration_session(runtime: Runtime, request: Request) -> RegistrationSession:
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
    return utcnow() + timedelta(seconds=runtime.settings.registration_session_ttl_seconds)


def _registration_session_is_expired(session: RegistrationSession) -> bool:
    """Compare one persisted expiry timestamp against current UTC safely."""
    # SQLite may hand timezone columns back as naive datetimes, so the
    # registration flow treats naive expiries as UTC instead of crashing.
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=utcnow().tzinfo)
    return expires_at <= utcnow()


def _apply_session_cookie(runtime: Runtime, response: Response, session_token: str) -> None:
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


def _html_response(*, title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """Wrap one small plain-HTML page used by the registration flow."""
    return HTMLResponse(
        (
            "<!doctype html>"
            "<html><head>"
            f"<title>{title}</title>"
            "<meta charset=\"utf-8\" />"
            "</head><body>"
            f"{body}"
            "</body></html>"
        ),
        status_code=status_code,
    )


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
    existing = runtime.database.event_receipts.get_event_receipt(event.delivery_id)
    if existing is None:
        runtime.database.event_receipts.create_event_receipt(
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
    if existing.status == "deferred":
        runtime.database.event_receipts.update_event_receipt(
            delivery_id=event.delivery_id,
            status="in_progress",
            detail="retrying deferred delivery",
        )
        return None

    runtime.database.event_receipts.update_event_receipt(
        delivery_id=event.delivery_id,
        status="in_progress",
        detail="retrying failed delivery",
    )
    return None


def _event_object_id(event: BridgeGatewayEvent) -> str:
    # Receipt tracking needs one stable object identifier even though follow
    # lifecycle events do not carry post/comment objects.
    if event.event_type in {"follow.accepted", "local.follow_requested"}:
        return event.object.follow_activity_id
    if event.event_type == "local.unfollow_requested":
        return event.object.follow_activity_id or event.delivery_id
    return event.object.ap_id


def _finish_event_processing(runtime: Runtime, delivery_id: str, status_value: str, detail: str) -> None:
    runtime.database.event_receipts.update_event_receipt(
        delivery_id=delivery_id,
        status=status_value,
        detail=detail,
    )


def _mark_event_failed(runtime: Runtime, delivery_id: str, detail: str) -> None:
    runtime.database.event_receipts.update_event_receipt(
        delivery_id=delivery_id,
        status="failed",
        detail=detail,
    )
