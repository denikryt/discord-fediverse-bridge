"""Source-stable canonical AP IDs, independent of ownership and mutable community."""
from __future__ import annotations
import unittest
from src.content_sync.outbound_publish import canonical_community_create_ids
from src.platform_types import MessageRef

class CommunityIdentityTests(unittest.TestCase):
    def test_replay_has_exact_same_id_and_page_note_have_different_paths(self):
        ref=MessageRef('discord-primary','200','300')
        a=canonical_community_create_ids(source=ref,actor_username='alice',actor_url='https://bridge.test/actors/users/alice',kind='post')
        b=canonical_community_create_ids(source=ref,actor_username='alice',actor_url='https://bridge.test/actors/users/alice',kind='post')
        self.assertEqual(a,b)
        self.assertEqual(len(a[0]),64)
        self.assertIn('/posts/'+a[0],a[1])
        comment=canonical_community_create_ids(source=ref,actor_username='alice',actor_url='https://bridge.test/actors/users/alice',kind='comment')
        self.assertEqual(a[0],comment[0])
        self.assertNotEqual(a[1],comment[1])

    def test_same_message_id_different_immediate_container_is_distinct(self):
        first=MessageRef('discord-primary','200','300')
        second=MessageRef('discord-primary','other-thread','300')
        third=MessageRef('fluxer-primary','200','300')
        def digest(m):return canonical_community_create_ids(source=m,actor_username='alice',actor_url='https://bridge.test/actors/users/alice',kind='post')[0]
        self.assertEqual(len({digest(first),digest(second),digest(third)}),3)

    def test_invalid_actor_and_untyped_source_fail(self):
        with self.assertRaises(ValueError):
            canonical_community_create_ids(source='discord-primary:200:300',actor_username='alice',actor_url='https://bridge.test',kind='post')
        with self.assertRaises(ValueError):
            canonical_community_create_ids(source=MessageRef('discord-primary','200','300'),actor_username='alice',actor_url='http://bridge.test',kind='post')
        with self.assertRaises(ValueError):
            canonical_community_create_ids(source=MessageRef('discord-primary','200','300'),actor_username='alice',actor_url='https://bridge.test',kind='like')

if __name__=='__main__':unittest.main(verbosity=2)
