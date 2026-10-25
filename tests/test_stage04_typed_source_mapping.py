"""Checkpoint 57 exact physical source resolution with real SQLite rows."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from src.db.database import Database
from src.platform_types import MessageRef
from src.models import MessageMapping, PublishedActivityObject


class ExactSourceMappingScenarios(unittest.TestCase):
    """Two immediate containers must not share one canonical source identity."""

    def setUp(self) -> None:
        """Initialize the same full v1 used by production startup."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(f"sqlite:///{Path(self.temp.name) / 'db.sqlite'}")
        self.addCleanup(self.db.close)
        self.db.migrate()

    def test_two_threads_same_message_id_are_distinct_sources(self) -> None:
        """Exact connector and container select one canonical AP mapping."""
        for i, container in enumerate(('thread-A','thread-B'),start=1):
            source = MessageRef('discord-primary', container, '777')
            self.db.message_mappings.create_message_mapping(
                source_platform='discord', source_id='777',
                source_message=source,
                activity_id=f'https://bridge.test/a/{i}',
                object_id=f'https://bridge.test/o/{i}',
                actor_url='https://bridge.test/actors/user/alice',
                community_actor_url='https://remote.test/c/news',
                discord_channel_id=None, discord_message_id=777,
            )
            mapping=self.db.message_mappings.get_by_source_ref(source)
            self.assertEqual(mapping.object_id,f'https://bridge.test/o/{i}')
        self.assertIsNone(self.db.message_mappings.get_by_source_ref(MessageRef('discord-primary','thread-C','777')))
        self.assertIsNone(self.db.message_mappings.get_by_source_ref(MessageRef('different-connector','thread-A','777')))
        with self.assertRaises(ValueError):
            self.db.message_mappings.get_message_mapping_by_source('discord','777')
        # Even a *single* typed source can never be read without a container.
        with self.assertRaises(ValueError):
            self.db.message_mappings.get_message_mapping_by_discord_message_id(777)
        with self.assertRaises(ValueError):
            self.db.message_mappings.create_message_mapping(
                source_platform='discord',source_id='not-777',
                source_message=MessageRef('discord-primary','thread-A','777'),
                activity_id='https://bridge.test/a/3',object_id='https://bridge.test/o/3',
                actor_url='https://bridge.test/actors/user/alice',
                community_actor_url='https://remote.test/c/news',
                discord_channel_id=None, discord_message_id=777,
            )

    def test_connector_collision_does_not_cross_platforms(self) -> None:
        """Opaque ID equality across registered connectors never gives one AP object."""
        from src.db.repositories.platform_surfaces import PlatformSurfaceRepository

        with self.db.immediate_session() as session:
            PlatformSurfaceRepository.add_connector(
                session, connector_key='fake-fluxer', platform='fluxer',
                instance_origin='https://fluxer.invalid',
            )
        first = self.db.message_mappings.create_message_mapping(
            source_platform='discord', source_id='same-id',
            source_message=MessageRef('discord-primary','thread-x','same-id'),
            activity_id='https://bridge.test/activities/discord',
            object_id='https://bridge.test/objects/discord',
            actor_url='https://bridge.test/actors/user/a',
            community_actor_url='https://remote.test/c/g',
            discord_channel_id=None,discord_message_id=None,
        )
        second = self.db.message_mappings.create_message_mapping(
            source_platform='fluxer', source_id='same-id',
            source_message=MessageRef('fake-fluxer','thread-x','same-id'),
            activity_id='https://bridge.test/activities/fluxer',
            object_id='https://bridge.test/objects/fluxer',
            actor_url='https://bridge.test/actors/user/a',
            community_actor_url='https://remote.test/c/g',
            discord_channel_id=None,discord_message_id=None,
        )
        self.assertNotEqual(first.id,second.id)
        self.assertEqual(self.db.message_mappings.get_by_source_ref(
            MessageRef('fake-fluxer','thread-x','same-id')).id, second.id)
        self.assertEqual(self.db.message_mappings.get_by_source_ref(
            MessageRef('discord-primary','thread-x','same-id')).id, first.id)
        with self.assertRaises(ValueError):
            self.db.message_mappings.create_message_mapping(
                source_platform='discord', source_id='same-id',
                source_message=MessageRef('fake-fluxer','thread-x','same-id'),
                activity_id='https://bridge.test/activities/other',
                object_id='https://bridge.test/objects/other',
                actor_url='https://bridge.test/actors/user/a',
                community_actor_url='https://remote.test/c/g',
                discord_channel_id=None,discord_message_id=None,
            )

    def test_frozen_canonical_mapping_does_not_adopt_second_container(self) -> None:
        """Replayed activity/object IDs cannot be reassigned to another source."""
        source=MessageRef('discord-primary','thread-A','777')
        data=dict(source_platform='discord',source_id='777',source_message=source,
                  activity_id='https://bridge.test/a/1',object_id='https://bridge.test/o/1',
                  actor_url='https://bridge.test/actors/user/alice',
                  community_actor_url='https://remote.test/c/news',
                  discord_channel_id=None,discord_message_id=777)
        before=self.db.message_mappings.create_message_mapping(**data)
        self.assertEqual(self.db.message_mappings.create_message_mapping(**data).id,before.id)
        with self.assertRaises(ValueError):
            self.db.message_mappings.create_message_mapping(**(data | {'source_message':MessageRef('discord-primary','thread-B','777')}))

    def test_published_object_lookup_is_exact_and_duplicate_rejected(self) -> None:
        """PublishedActivityObject indexes the same typed source tuple."""
        with self.db.session() as session:
            for i, container in enumerate(('thread-A','thread-B'),start=1):
                session.add(PublishedActivityObject(
                    source_connector_id=1,source_external_container_id=container,
                    source_external_message_id='777',
                    actor_username='alice',actor_url='https://bridge.test/actors/user/alice',
                    publication_context='community',community_actor_url='https://remote.test/c/news',
                    activity_id=f'https://bridge.test/a/{i}',object_id=f'https://bridge.test/o/{i}',
                    kind='comment',body_markdown='body',discord_message_id=777,
                ))
        for i, container in enumerate(('thread-A','thread-B'),start=1):
            self.assertEqual(self.db.activitypub_objects.get_by_source_ref(
                MessageRef('discord-primary',container,'777')).object_id,f'https://bridge.test/o/{i}')
        self.assertIsNone(self.db.activitypub_objects.get_by_source_ref(MessageRef('discord-primary','unknown','777')))
        with self.assertRaises(ValueError):
            self.db.activitypub_objects.get_published_activity_object_by_discord_message_id(777)
        with self.assertRaises(IntegrityError):
            with self.db.session() as session:
                session.add(PublishedActivityObject(
                    source_connector_id=1,source_external_container_id='thread-A',
                    source_external_message_id='777',
                    actor_username='alice',actor_url='https://bridge.test/actors/user/alice',
                    publication_context='community',community_actor_url='https://remote.test/c/news',
                    activity_id='https://bridge.test/a/3',object_id='https://bridge.test/o/3',
                    kind='comment',body_markdown='body',discord_message_id=777,
                ))


if __name__ == '__main__':
    unittest.main(verbosity=2)
