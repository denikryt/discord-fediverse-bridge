from __future__ import annotations

from sqlalchemy import select

from ...models import (
    CommentLink,
    CommunityMessageGroup,
    CommunityMessageGroupDelivery,
    CommunityThreadGroup,
    CommunityThreadGroupDelivery,
    PostLink,
)
from .base import BaseRepository


"""Legacy Lemmy post/comment mapping persistence."""


class LegacyLemmyMappingRepository(BaseRepository):
    """Persist the legacy lemmy mappings domain."""

    def get_post_link_by_thread_id(self, discord_thread_id: int) -> PostLink | None:
            """Load the post-link row for one Discord thread, if it exists."""
            with self.session() as session:
                return session.scalar(select(PostLink).where(PostLink.discord_forum_thread_id == discord_thread_id))

    def get_post_link_by_lemmy_post_id(self, lemmy_post_id: int) -> PostLink | None:
            """Load the post-link row for one numeric Lemmy post ID."""
            with self.session() as session:
                return session.scalar(select(PostLink).where(PostLink.lemmy_post_id == lemmy_post_id))

    def get_post_link_by_lemmy_post_ap_id(self, lemmy_post_ap_id: str) -> PostLink | None:
            """Load one post-link row for an ActivityPub Lemmy post ID.

            This compatibility helper returns the first matching row. Newer
            multi-target inbound routing should prefer
            `get_post_links_by_lemmy_post_ap_id()`.
            """
            with self.session() as session:
                return session.scalar(select(PostLink).where(PostLink.lemmy_post_ap_id == lemmy_post_ap_id))

    def get_post_links_by_lemmy_post_ap_id(self, lemmy_post_ap_id: str) -> list[PostLink]:
            """Load every post-link row for one ActivityPub Lemmy post ID."""
            # Multi-channel inbound fanout needs every Discord thread that already
            # represents the same remote post, not just the first row.
            with self.session() as session:
                return list(
                    session.scalars(
                        select(PostLink).where(PostLink.lemmy_post_ap_id == lemmy_post_ap_id)
                    )
                )

    def get_post_link_by_lemmy_post_ap_id_and_channel_id(
            self,
            lemmy_post_ap_id: str,
            discord_forum_channel_id: int,
        ) -> PostLink | None:
            """Load the post-link row for one remote post delivered into one forum channel."""
            # Fanout retries must be able to see whether a specific subscribed
            # target channel already received the remote post.
            with self.session() as session:
                return session.scalar(
                    select(PostLink).where(
                        PostLink.lemmy_post_ap_id == lemmy_post_ap_id,
                        PostLink.discord_forum_channel_id == discord_forum_channel_id,
                    )
                )

    def create_post_link(
            self,
            *,
            lemmy_post_id: int,
            lemmy_post_ap_id: str | None,
            discord_forum_channel_id: int | None = None,
            discord_forum_thread_id: int,
            discord_starter_message_id: int | None,
            direction: str,
        ) -> PostLink:
            """Persist the thread-level mapping created by the existing bridge."""
            with self.session() as session:
                link = PostLink(
                    lemmy_post_id=lemmy_post_id,
                    lemmy_post_ap_id=lemmy_post_ap_id,
                    discord_forum_channel_id=discord_forum_channel_id,
                    discord_forum_thread_id=discord_forum_thread_id,
                    discord_starter_message_id=discord_starter_message_id,
                    direction=direction,
                )
                session.add(link)
                session.flush()
                return link

    def has_comment_link_for_discord_message(self, discord_message_id: int) -> bool:
            """Return whether one Discord message is already mapped as a comment."""
            with self.session() as session:
                return session.scalar(select(CommentLink.id).where(CommentLink.discord_message_id == discord_message_id)) is not None

    def has_comment_link_for_lemmy_comment(self, lemmy_comment_id: int) -> bool:
            """Return whether one Lemmy comment is already mapped into Discord."""
            with self.session() as session:
                return session.scalar(select(CommentLink.id).where(CommentLink.lemmy_comment_id == lemmy_comment_id)) is not None

    def get_comment_link_by_lemmy_comment_ap_id(self, lemmy_comment_ap_id: str) -> CommentLink | None:
            """Load one comment-link row for one ActivityPub comment ID.

            This compatibility helper returns the first matching row. Newer
            multi-target inbound routing should prefer
            `get_comment_links_by_lemmy_comment_ap_id()`.
            """
            with self.session() as session:
                return session.scalar(select(CommentLink).where(CommentLink.lemmy_comment_ap_id == lemmy_comment_ap_id))

    def get_comment_links_by_lemmy_comment_ap_id(self, lemmy_comment_ap_id: str) -> list[CommentLink]:
            """Load every comment-link row for one ActivityPub comment ID."""
            # Fanout routing and parent resolution both need to see all thread-local
            # copies of the same remote comment.
            with self.session() as session:
                return list(
                    session.scalars(
                        select(CommentLink).where(CommentLink.lemmy_comment_ap_id == lemmy_comment_ap_id)
                    )
                )

    def get_comment_link_by_discord_message_id(self, discord_message_id: int) -> CommentLink | None:
            """Load the comment-link row for one Discord message ID."""
            with self.session() as session:
                return session.scalar(select(CommentLink).where(CommentLink.discord_message_id == discord_message_id))

    def create_comment_link(
            self,
            *,
            lemmy_comment_id: int,
            lemmy_comment_ap_id: str | None,
            lemmy_parent_comment_ap_id: str | None,
            lemmy_post_id: int,
            discord_forum_thread_id: int,
            discord_message_id: int,
            direction: str,
        ) -> CommentLink:
            """Persist the message-level mapping created by the existing bridge."""
            with self.session() as session:
                link = CommentLink(
                    lemmy_comment_id=lemmy_comment_id,
                    lemmy_comment_ap_id=lemmy_comment_ap_id,
                    lemmy_parent_comment_ap_id=lemmy_parent_comment_ap_id,
                    lemmy_post_id=lemmy_post_id,
                    discord_forum_thread_id=discord_forum_thread_id,
                    discord_message_id=discord_message_id,
                    direction=direction,
                )
                session.add(link)
                session.flush()
                return link
