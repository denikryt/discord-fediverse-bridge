from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class LemmyAPIError(RuntimeError):
    def __init__(self, message: str, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class LemmyClient:
    # LemmyClient hides authentication, retry, and error-shape quirks of the
    # Lemmy API behind a small async interface used by the bridge.
    def __init__(self, base_url: str, username_or_email: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username_or_email = username_or_email
        self.password = password
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=30.0),
        )
        self.jwt: str | None = None
        self.person_id: int | None = None
        self.person_name: str | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def login(self) -> None:
        # Login also loads the current user because later bridge flows rely on
        # stable numeric/user identity.
        response = await self._request_with_retry(
            "POST",
            "/api/v3/user/login",
            json={
                "username_or_email": self.username_or_email,
                "password": self.password,
            },
            error_message="Lemmy login failed",
            operation_name="Lemmy login",
        )
        payload = response.json()
        self.jwt = payload["jwt"]
        await self.load_current_user()

    async def ensure_login(self) -> None:
        if self.jwt is None:
            await self.login()

    async def load_current_user(self) -> None:
        await self.ensure_login()
        response = await self._request_with_retry(
            "GET",
            "/api/v3/site",
            headers=self._headers(),
            error_message="Loading current Lemmy user failed",
            operation_name="Lemmy site fetch",
        )
        payload = response.json()
        my_user = payload.get("my_user") or {}
        local_user = my_user.get("local_user_view") or {}
        person = local_user.get("person") or {}
        self.person_id = int(person["id"])
        self.person_name = person["name"]

    async def create_post(self, *, community_id: int, name: str, body: str | None) -> dict[str, Any]:
        await self.ensure_login()
        response = await self._request_with_retry(
            "POST",
            "/api/v3/post",
            headers=self._headers(),
            json={
                "community_id": community_id,
                "name": name,
                "body": body,
                "honeypot": None,
            },
            error_message="Creating Lemmy post failed",
            operation_name="Lemmy create post",
        )
        return response.json()

    async def resolve_community_id(self, *, name: str) -> int:
        response = await self._request_with_retry(
            "GET",
            "/api/v3/community",
            params={"name": name},
            headers=self._headers(optional=True),
            error_message="Resolving Lemmy community failed",
            operation_name="Lemmy resolve community",
        )
        payload = response.json()
        community_view = payload.get("community_view") or {}
        community = community_view.get("community") or {}
        community_id = community.get("id")
        if community_id is None:
            raise RuntimeError(f"Could not resolve Lemmy community ID for name '{name}'")
        return int(community_id)

    async def create_comment(self, *, post_id: int, content: str, parent_id: int | None = None) -> dict[str, Any]:
        await self.ensure_login()
        payload: dict[str, Any] = {
            "post_id": post_id,
            "content": content,
        }
        if parent_id is not None:
            payload["parent_id"] = parent_id
        response = await self._request_with_retry(
            "POST",
            "/api/v3/comment",
            headers=self._headers(),
            json=payload,
            error_message="Creating Lemmy comment failed",
            operation_name="Lemmy create comment",
        )
        return response.json()

    async def list_posts(
        self,
        *,
        community_id: int | None = None,
        community_name: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "sort": "New",
            "limit": limit,
        }
        if community_id is not None:
            params["community_id"] = community_id
        if community_name is not None:
            params["community_name"] = community_name
        response = await self._request_with_retry(
            "GET",
            "/api/v3/post/list",
            params=params,
            headers=self._headers(optional=True),
            error_message="Listing Lemmy posts failed",
            operation_name="Lemmy list posts",
        )
        payload = response.json()
        return payload.get("posts", [])

    async def list_comments(
        self,
        *,
        community_id: int | None = None,
        community_name: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "type_": "All",
            "limit": limit,
        }
        if community_id is not None:
            params["community_id"] = community_id
        if community_name is not None:
            params["community_name"] = community_name
        response = await self._request_with_retry(
            "GET",
            "/api/v3/comment/list",
            params=params,
            headers=self._headers(optional=True),
            error_message="Listing Lemmy comments failed",
            operation_name="Lemmy list comments",
        )
        payload = response.json()
        return payload.get("comments", [])

    def _headers(self, optional: bool = False) -> dict[str, str]:
        if self.jwt:
            return {"Authorization": f"Bearer {self.jwt}"}
        if optional:
            return {}
        raise RuntimeError("Lemmy client is not authenticated")

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        error_message: str,
        operation_name: str,
        **kwargs: Any,
    ) -> httpx.Response:
        # Lemmy rate-limits and occasionally times out on repeated test traffic,
        # so client calls share one conservative retry policy.
        backoff_seconds = [0, 10, 30, 60]

        for attempt, delay in enumerate(backoff_seconds, start=1):
            if delay:
                logger.warning("%s retrying in %ss", operation_name, delay)
                await asyncio.sleep(delay)

            try:
                response = await self.client.request(method, url, **kwargs)
            except httpx.ReadTimeout:
                if attempt < len(backoff_seconds):
                    logger.warning("%s timed out, will retry", operation_name)
                    continue
                raise RuntimeError(f"{operation_name} failed: read_timeout")

            try:
                self._raise_for_status_with_lemmy_error(response, error_message)
            except LemmyAPIError as exc:
                if exc.error_code == "rate_limit_error" and attempt < len(backoff_seconds):
                    logger.warning("%s rate-limited, will retry", operation_name)
                    continue
                raise

            return response

        raise RuntimeError(f"{operation_name} failed after retries")

    @staticmethod
    def _raise_for_status_with_lemmy_error(response: httpx.Response, message: str) -> None:
        # Lemmy reports many API failures as JSON error payloads with 4xx
        # status codes, so preserve that semantic error code for callers.
        if response.is_success:
            return
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("error"):
            raise LemmyAPIError(f"{message}: {payload['error']}", error_code=str(payload["error"]))
        response.raise_for_status()
