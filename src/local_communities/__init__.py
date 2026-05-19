"""Local-community domain for Discord-backed federated community hosting.

This package owns the second bridge mode where one Discord forum channel is
published as one local ActivityPub community surface. It intentionally stays
separate from `community_sync`, which models the inverse subscription flow.
"""

from .runtime import LocalCommunityRuntime
from .service import LocalCommunityService, LocalCommunityError

__all__ = [
    "LocalCommunityError",
    "LocalCommunityRuntime",
    "LocalCommunityService",
]
