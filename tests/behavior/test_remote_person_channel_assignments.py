"""Real writer-transaction scenarios for shared remote Person feed destinations."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from src.db import Database
from src.channel_ownership import resolve_channel_owner
from src.models import (
    User, RemotePersonFollow, RemotePersonPost, RemotePersonPostDelivery,
    RemotePersonCommentPlacement, InboundConversationObject, PersonalFeedBinding,
)
from src.personal_feeds import PersonalFeedService
from src.remote_person_feeds import RemotePersonChannelService, REQUIRED_BOT_PERMISSIONS

REMOTE = "https://remote.example/users/bob"


def permissions(**override: bool) -> SimpleNamespace:
    """Construct effective Discord channel flags without invoking the SDK."""
    return SimpleNamespace(**{name: override.get(name, True) for name in REQUIRED_BOT_PERMISSIONS})


class RemoteChannelAssignmentScenarios(unittest.TestCase):
    """Exercise exact guild/user ownership and last-link queue cancellation."""

    def setUp(self) -> None:
        """Prepare three registered user identities and a migrated real database."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(f"sqlite:///{Path(tmp.name) / 'bridge.db'}")
        self.addCleanup(self.db.close)
        self.db.migrate()
        self.remote = RemotePersonChannelService(self.db)
        self.personal = PersonalFeedService(self.db)
        with self.db.session() as session:
            for user in (101, 102, 103):
                actor = f"https://bridge.example/actors/users/{user}"
                session.add(User(
                    discord_user_id=str(user), activitypub_username=f"u{user}", actor_url=actor,
                    inbox_url=f"{actor}/inbox", outbox_url=f"{actor}/outbox",
                    followers_url=f"{actor}/followers", public_key_pem="public", private_key_pem="private",
                ))
        with self.db.session() as session:
            for owner in (101, 102, 103):
                actor_id = session.scalar(select(User.id).where(User.discord_user_id == str(owner)))
                session.add(RemotePersonFollow(
                    user_id=actor_id, remote_actor_id=REMOTE,
                    remote_inbox_url=f"{REMOTE}/inbox",
                    follow_activity_id=f"https://bridge.example/follows/{owner}",
                    generation=1, relationship_status="accepted", follow_dispatch_state="sent",
                ))

    def add(self, owner: int, channel: int = 111, **kwargs):
        """Perform one authorized assignment with explicit channel facts."""
        return self.remote.add_channel(
            discord_user_id=str(owner), remote_actor_id=REMOTE,
            discord_guild_id=1, discord_channel_id=channel,
            target_channel_guild_id=kwargs.get("guild_id", 1),
            channel_type=kwargs.get("type", "text"),
            caller_can_manage_guild=kwargs.get("manager", True),
            bot_permissions=kwargs.get("perms", permissions()),
        )

    def remove(self, owner: int, channel: int = 111):
        """Remove exactly one owner/channel assignment without Manage Server."""
        return self.remote.remove_channel(discord_user_id=str(owner),
            remote_actor_id=REMOTE, discord_guild_id=1, discord_channel_id=channel)

    def post_and_pending_comment(self, channel: int = 111, *, root_state: str = "delivered"):
        """Reserve one root and one bot comment for last-link cancellation checks."""
        with self.db.session() as session:
            post = RemotePersonPost(
                object_id=f"{REMOTE}/posts/{channel}", remote_actor_id=REMOTE,
                content_markdown="hello", source_activity_json="{}", revision_fingerprint="a"*64,
            )
            session.add(post)
            session.flush()
            root = RemotePersonPostDelivery(
                post_id=post.id, discord_guild_id=1, discord_channel_id=channel,
                state=root_state,
                discord_root_message_id=(400 + channel if root_state == "delivered" else None),
                discord_thread_id=(400 + channel if root_state == "delivered" else None),
            )
            session.add(root)
            session.flush()
            inbound = InboundConversationObject(
                object_id=f"{REMOTE}/comments/{channel}",
                conversation_root_object_id=post.object_id, parent_object_id=post.object_id,
                actor_id=REMOTE, content_markdown="reply", revision_fingerprint="b"*64,
            )
            session.add(inbound)
            session.flush()
            session.add(RemotePersonCommentPlacement(
                root_delivery_id=root.id, inbound_object_id=inbound.id,
                state="pending_parent",
            ))
            return root.id

    def test_two_users_share_one_destination_without_exclusive_claim(self) -> None:
        """Shared feeds coexist and duplicate assignments are idempotent."""
        first = self.add(101)
        second = self.add(102)
        self.assertEqual(first.status, "assigned")
        self.assertEqual(second.status, "assigned")
        self.assertNotEqual(first.follow_id, second.follow_id)
        self.assertEqual(self.add(101).status, "already_assigned")
        with self.db.session() as session:
            self.assertEqual(resolve_channel_owner(session, 111).kind, "remote_person_feed")
        self.assertEqual(self.personal.bind_or_move("103", 1, 111).status, "channel_conflict")

    def test_one_channel_accepts_many_distinct_remote_people(self) -> None:
        """Shared destinations are keyed by Follow rather than by channel ID."""
        other = "https://remote.example/users/charlie"
        with self.db.session() as session:
            user_id = session.scalar(select(User.id).where(User.discord_user_id == "101"))
            session.add(RemotePersonFollow(
                user_id=user_id, remote_actor_id=other,
                remote_inbox_url=f"{other}/inbox",
                follow_activity_id="https://bridge.example/follows/charlie",
                generation=1, relationship_status="accepted", follow_dispatch_state="sent",
            ))
        self.assertEqual(self.add(101).status, "assigned")
        independent = self.remote.add_channel(
            discord_user_id="101", remote_actor_id=other,
            discord_guild_id=1, discord_channel_id=111, target_channel_guild_id=1,
            channel_type="text", caller_can_manage_guild=True,
            bot_permissions=permissions(),
        )
        self.assertEqual(independent.status, "assigned")
        self.assertNotEqual(self.add(101).follow_id, independent.follow_id)

    def test_personal_feed_then_remote_feed_is_refused(self) -> None:
        """An existing exclusive personal root cannot turn into shared delivery."""
        self.assertEqual(self.personal.bind_or_move("103", 1, 111).status, "bound")
        denied = self.add(101)
        self.assertEqual(denied.status, "channel_conflict")
        self.assertEqual(denied.conflict_kind, "personal_feed")
        self.assertEqual(self.personal.get_by_discord_user_id("103").discord_channel_id, 111)

    def test_discord_channel_capabilities_and_owner_rights_are_mandatory(self) -> None:
        """Cannot route into foreign guild, forbidden channel type, or unreadable thread."""
        self.assertEqual(self.add(101, guild_id=2).status, "invalid_target")
        self.assertEqual(self.add(101, type="forum").status, "invalid_target")
        self.assertEqual(self.add(101, manager=False).status, "forbidden")
        for flag in REQUIRED_BOT_PERMISSIONS:
            denied = self.add(101, perms=permissions(**{flag: False}))
            self.assertEqual(denied.status, "missing_bot_permissions")
            self.assertIn(flag, denied.missing_permissions)
        self.assertEqual(self.add(101).status, "assigned")

    def test_only_owner_can_remove_an_assignment(self) -> None:
        """Presenting another user's destination never deletes their Follow."""
        self.assertEqual(self.add(101).status, "assigned")
        self.assertEqual(self.remove(102).status, "not_assigned")
        self.assertEqual(self.remove(101).status, "removed")
        self.assertEqual(self.remove(101).status, "not_assigned")

    def test_last_assignment_suppresses_old_work_before_new_user_subscribes(self) -> None:
        """A-era unclaimed bot jobs never become B-era backfill after rejoin."""
        self.add(101)
        root_id = self.post_and_pending_comment(root_state="pending")
        self.assertEqual(self.remove(101).status, "removed")
        self.add(102)
        with self.db.session() as session:
            root = session.get(RemotePersonPostDelivery, root_id)
            placement = session.scalar(select(RemotePersonCommentPlacement))
            self.assertEqual(root.state, "suppressed")
            self.assertEqual(placement.state, "suppressed")
            self.assertIsNone(root.claim_token)

    def test_other_accepted_owner_prevents_premature_queue_suppression(self) -> None:
        """Removing A while B stays accepted keeps the shared channel active."""
        self.add(101)
        self.add(102)
        root_id = self.post_and_pending_comment(root_state="pending")
        self.remove(101)
        with self.db.session() as session:
            self.assertEqual(session.get(RemotePersonPostDelivery, root_id).state, "pending")
            self.assertEqual(session.scalar(select(RemotePersonCommentPlacement.state)), "pending_parent")
        self.remove(102)
        with self.db.session() as session:
            self.assertEqual(session.get(RemotePersonPostDelivery, root_id).state, "suppressed")

    def test_existing_delivered_root_kept_while_pending_comment_cancelled(self) -> None:
        """Retained copies may still receive Update/Delete after unsubscribe."""
        self.add(101)
        root_id = self.post_and_pending_comment(root_state="delivered")
        self.remove(101)
        with self.db.session() as session:
            self.assertEqual(session.get(RemotePersonPostDelivery, root_id).state, "delivered")
            self.assertEqual(session.scalar(select(RemotePersonCommentPlacement.state)), "suppressed")

    def test_competing_personal_and_remote_claims_cannot_both_win(self) -> None:
        """Both exclusive and shared admissions contend under BEGIN IMMEDIATE."""
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        barrier = Barrier(2)

        def claim_personal():
            """Use the normal exclusive bind in the competing worker thread."""
            barrier.wait()
            return self.personal.bind_or_move("103", 1, 111)

        def claim_remote():
            """Use the remote assignment service's distinct writer reservation."""
            barrier.wait()
            return self.add(101)

        with ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(claim_personal)
            b = executor.submit(claim_remote)
            personal = a.result()
            remote = b.result()
        winners = int(personal.status == "bound") + int(remote.status == "assigned")
        self.assertEqual(winners, 1)
        self.assertIn(personal.status, {"bound", "channel_conflict"})
        self.assertIn(remote.status, {"assigned", "channel_conflict"})

    def test_sending_root_is_not_misclassified_as_unsent(self) -> None:
        """One already-dispatched HTTP attempt may finish after unbind."""
        self.add(101)
        root_id = self.post_and_pending_comment(root_state="sending")
        self.remove(101)
        with self.db.session() as session:
            self.assertEqual(session.get(RemotePersonPostDelivery, root_id).state, "sending")


if __name__ == "__main__":
    unittest.main()
