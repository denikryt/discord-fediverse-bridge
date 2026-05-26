"""Read-only Lemmy HTTP API client used for remote community lookup."""

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
    # LemmyClient is now a small public-API wrapper used only for read-only
    # community lookup and autocomplete. It no longer owns account login.
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=30.0),
        )

    async def close(self) -> None:
        """Close the shared HTTP client used for public Lemmy API calls."""
        await self.client.aclose()

    async def resolve_community_id(self, *, name: str) -> int:
        """Resolve one public Lemmy community name into its numeric ID."""
        community = await self.resolve_community(name=name)
        community_id = community.get("id")
        if community_id is None:
            raise RuntimeError(f"Could not resolve Lemmy community ID for name '{name}'")
        return int(community_id)

    async def resolve_community(self, *, name: str) -> dict[str, Any]:
        """Resolve one public Lemmy community name into its public community object.

        The unified discovery layer needs the canonical actor URL and numeric id
        for direct remote handles and URLs, so this helper returns the public
        `community` object from Lemmy's `/api/v3/community` endpoint.
        """
        response = await self._request_with_retry(
            "GET",
            "/api/v3/community",
            params={"name": name},
            error_message="Resolving Lemmy community failed",
            operation_name="Lemmy resolve community",
        )
        payload = response.json()
        community_view = payload.get("community_view") or {}
        community = community_view.get("community") or {}
        if not community:
            raise RuntimeError(f"Could not resolve Lemmy community '{name}'")
        return community

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
            error_message="Listing Lemmy posts failed",
            operation_name="Lemmy list posts",
        )
        payload = response.json()
        return payload.get("posts", [])

    async def list_communities(self, *, limit: int = 50, type_: str = "All") -> list[dict[str, Any]]:
        # Returns public community_view dicts; each contains
        # community.actor_id, community.name, community.id, and community.title.
        response = await self._request_with_retry(
            "GET",
            "/api/v3/community/list",
            params={"limit": limit, "type_": type_, "sort": "TopAll"},
            error_message="Listing Lemmy communities failed",
            operation_name="Lemmy list communities",
        )
        return response.json().get("communities", [])

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
            error_message="Listing Lemmy comments failed",
            operation_name="Lemmy list comments",
        )
        payload = response.json()
        return payload.get("comments", [])

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
