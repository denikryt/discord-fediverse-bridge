"""Discord OAuth client used by the public web registration flow."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from .config import Settings


@dataclass(slots=True)
class DiscordUserProfile:
    """Store the subset of Discord profile fields the bridge registration needs."""

    user_id: str
    username: str
    avatar_url: str | None


class DiscordOAuthClient:
    """Wrap the Discord OAuth and user-profile HTTP boundary."""

    # The registration handlers only need three boundary operations: build the
    # authorize URL, exchange the callback code, and load the authenticated
    # user profile. Keeping them here makes route tests easy to fake.
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=10.0)

    def build_authorization_url(self, state: str) -> str:
        """Build the Discord authorize URL for one registration session."""
        query = urlencode(
            {
                "client_id": self.settings.discord_oauth_client_id,
                "redirect_uri": self.settings.discord_oauth_redirect_uri,
                "response_type": "code",
                "scope": "identify",
                "state": state,
                "prompt": "consent",
            }
        )
        return f"https://discord.com/api/oauth2/authorize?{query}"

    async def exchange_code_for_access_token(self, code: str) -> str:
        """Exchange one OAuth callback code for a Discord access token."""
        response = await self._client.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": self.settings.discord_oauth_client_id,
                "client_secret": self.settings.discord_oauth_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.discord_oauth_redirect_uri,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = response.json()
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Discord token exchange did not return an access token")
        return access_token

    async def fetch_user_profile(self, access_token: str) -> DiscordUserProfile:
        """Fetch the authenticated Discord user profile for registration."""
        response = await self._client.get(
            "https://discord.com/api/users/@me",
            headers={"authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        payload = response.json()
        user_id = payload.get("id")
        username = payload.get("username")
        avatar = payload.get("avatar")
        if not isinstance(user_id, str) or not user_id:
            raise RuntimeError("Discord user response did not include an id")
        if not isinstance(username, str) or not username:
            raise RuntimeError("Discord user response did not include a username")
        avatar_url = None
        if isinstance(avatar, str) and avatar:
            avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png"
        return DiscordUserProfile(
            user_id=user_id,
            username=username,
            avatar_url=avatar_url,
        )

    async def close(self) -> None:
        """Close the shared HTTP client used for Discord OAuth calls."""
        await self._client.aclose()
