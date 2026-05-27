"""Domain repositories for bridge persistence.

Repositories share Database session ownership; they do not create engines or
session factories.
"""

from .local_communities import LocalCommunityRepository
from .remote_subscribers import RemoteSubscriberRepository
from .local_subscribers import LocalSubscriberRepository
from .local_community_content import LocalCommunityContentRepository
from .local_community_surfaces import LocalCommunitySurfaceRepository
from .local_community_relay import LocalCommunityRelayRepository

__all__ = [
    "LocalCommunityRepository",
    "RemoteSubscriberRepository",
    "LocalSubscriberRepository",
    "LocalCommunityContentRepository",
    "LocalCommunitySurfaceRepository",
    "LocalCommunityRelayRepository",
]
