"""Seed synthetic migration history using only the authentic v0.1.0 ORM.

Run in a subprocess whose working directory contains the released src package.
The output path is explicit; no current-schema model participates in seeding.
"""
from pathlib import Path
from src.db.database import Database
from src import models as m
from sqlalchemy.orm import Session
import sys

path=Path(sys.argv[1])
if path.exists(): path.unlink()
db=Database(f'sqlite:///{path}')
db.create_all()
with Session(db.engine) as s:
    s.add_all([
        m.User(id=7,discord_user_id='1001',activitypub_username='alice',actor_url='https://bridge.test/actors/users/alice',inbox_url='https://bridge.test/actors/users/alice/inbox',outbox_url='https://bridge.test/actors/users/alice/outbox',followers_url='https://bridge.test/actors/users/alice/followers',public_key_pem='alice-PUB-\n',private_key_pem='alice-SECRET-\x00-Q'),
        m.User(id=8,discord_user_id='1002',activitypub_username='bob',actor_url='https://bridge.test/actors/users/bob',inbox_url='https://bridge.test/actors/users/bob/inbox',outbox_url='https://bridge.test/actors/users/bob/outbox',followers_url='https://bridge.test/actors/users/bob/followers',public_key_pem='bob-PUB-',private_key_pem='bob-SECRET'),
        m.LocalCommunity(id=10,discord_guild_id=1,discord_forum_channel_id=900,slug='news',display_name='News',actor_url='https://bridge.test/actors/communities/news',inbox_url='https://bridge.test/actors/communities/news/inbox',outbox_url='https://bridge.test/actors/communities/news/outbox',followers_url='https://bridge.test/actors/communities/news/followers',public_key_pem='community-public',private_key_pem='community-private-\x00',status='active'),
        m.LocalSubscriber(id=11,local_community_id=10,discord_guild_id=1,discord_channel_id=901,status='active'),
        m.LocalSubscriber(id=12,local_community_id=10,discord_guild_id=1,discord_channel_id=905,status='inactive'),
        m.ChannelCommunitySubscription(id=20,discord_guild_id=1,discord_channel_id=902,lemmy_community_actor_id='https://remote.test/c/news',follow_activity_id='https://bridge.test/follows/20',community_inbox_url='https://remote.test/c/news/inbox',status='accepted'),
        m.ChannelCommunitySubscription(id=21,discord_guild_id=1,discord_channel_id=903,lemmy_community_actor_id='https://remote.test/c/news',status='accepted'),
        m.ChannelCommunitySubscription(id=22,discord_guild_id=1,discord_channel_id=904,lemmy_community_actor_id='https://remote.test/c/news',status='failed'),
        m.BridgeActorFollow(id=23,community_actor_id='https://remote.test/c/news',follow_activity_id='https://bridge.test/follows/23',status='accepted'),
        m.BridgeActorKey(id=1,actor_url='https://bridge.test/actors/services/bridge',key_id='https://bridge.test/actors/services/bridge#main-key',key_format='pem',algorithm='rsa',public_key_data='svc-PUBLIC-\x00',private_key_data='svc-SECRET-\x00\n'),
        m.RemoteActor(id=25,actor_url='https://remote.test/u/bob',inbox_url='https://remote.test/u/bob/inbox',public_key_pem='remote-public'),
        m.RemoteSubscriber(id=26,local_community_id=10,remote_actor_id='https://remote.test/u/bob',remote_inbox_url='https://remote.test/u/bob/inbox',follow_activity_id='https://remote.test/follows/26',status='accepted'),
        m.RemoteSubscriber(id=27,local_community_id=10,remote_actor_id='https://remote.test/u/carol',remote_inbox_url='https://remote.test/u/carol/inbox',follow_activity_id='https://remote.test/follows/27',status='pending'),
        m.CommunityThreadGroup(id=30,community_actor_id='https://remote.test/c/news',source_channel_id=902,source_thread_id=1000,source_starter_message_id=1000,ap_activity_id='https://remote.test/a/create/30',ap_object_id='https://remote.test/o/30'),
        m.CommunityThreadGroupDelivery(id=31,thread_group_id=30,discord_channel_id=902,discord_thread_id=1000,discord_starter_message_id=1000,role='source'),
        m.CommunityThreadGroupDelivery(id=32,thread_group_id=30,discord_channel_id=903,discord_thread_id=2000,discord_starter_message_id=2001,role='subscriber'),
        m.CommunityMessageGroup(id=33,community_actor_id='https://remote.test/c/news',thread_group_id=30,source_channel_id=902,source_thread_id=1000,source_message_id=1002,ap_activity_id='https://remote.test/a/create/33',ap_object_id='https://remote.test/o/33'),
        m.CommunityMessageGroup(id=34,community_actor_id='https://remote.test/c/news',thread_group_id=30,source_channel_id=902,source_thread_id=1000,source_message_id=1003,ap_activity_id='https://remote.test/a/create/34',ap_object_id='https://remote.test/o/34',parent_message_group_id=33),
        m.CommunityMessageGroupDelivery(id=35,message_group_id=33,discord_channel_id=902,discord_thread_id=1000,discord_message_id=1002,role='source'),
        m.CommunityMessageGroupDelivery(id=36,message_group_id=33,discord_channel_id=903,discord_thread_id=2000,discord_message_id=2002,role='subscriber'),
        m.CommunityMessageGroupDelivery(id=37,message_group_id=34,discord_channel_id=902,discord_thread_id=1000,discord_message_id=1003,role='source'),
        m.LocalCommunityThread(id=40,local_community_id=10,ap_activity_id='https://bridge.test/actors/users/alice/activities/create/posts/40',ap_object_id='https://bridge.test/actors/users/alice/objects/posts/40',direction='outbound',origin_kind='local_host'),
        m.LocalCommunityThreadSurface(id=41,local_community_thread_id=40,discord_forum_channel_id=900,discord_thread_id=3000,discord_starter_message_id=3001,role='host'),
        m.LocalCommunityThreadSurface(id=42,local_community_thread_id=40,discord_forum_channel_id=901,discord_thread_id=3100,discord_starter_message_id=3101,local_subscriber_id=11,role='local_subscriber'),
        m.LocalCommunityMessage(id=43,local_community_thread_id=40,ap_activity_id='https://bridge.test/actors/users/alice/activities/create/comments/43',ap_object_id='https://bridge.test/actors/users/alice/objects/comments/43',parent_ap_object_id='https://bridge.test/actors/users/alice/objects/posts/40',direction='outbound'),
        m.LocalCommunityMessage(id=44,local_community_thread_id=40,ap_activity_id='https://bridge.test/actors/users/alice/activities/create/comments/44',ap_object_id='https://bridge.test/actors/users/alice/objects/comments/44',parent_ap_object_id='https://bridge.test/actors/users/alice/objects/comments/43',direction='outbound'),
        m.LocalCommunityMessageSurface(id=45,local_community_message_id=43,local_community_thread_surface_id=41,discord_forum_channel_id=900,discord_message_id=3002,parent_discord_message_id=3001,role='host'),
        m.LocalCommunityMessageSurface(id=46,local_community_message_id=43,local_community_thread_surface_id=42,discord_forum_channel_id=901,discord_message_id=3102,parent_discord_message_id=3101,local_subscriber_id=11,role='local_subscriber'),
        m.LocalCommunityMessageSurface(id=47,local_community_message_id=44,local_community_thread_surface_id=41,discord_forum_channel_id=900,discord_message_id=3003,parent_discord_message_id=3002,role='host'),
        m.PublishedActivityObject(id=50,actor_username='alice',actor_url='https://bridge.test/actors/users/alice',community_actor_url='https://remote.test/c/news',activity_id='https://bridge.test/actors/users/alice/activities/create/posts/50',object_id='https://bridge.test/actors/users/alice/objects/posts/50',kind='post',title='Unicode 🧪',body_markdown='hello _world_ 😎\nline2',discord_channel_id=902,discord_message_id=5000),
        m.PublishedActivityObject(id=51,actor_username='alice',actor_url='https://bridge.test/actors/users/alice',community_actor_url='https://remote.test/c/news',activity_id='https://bridge.test/actors/users/alice/activities/create/comments/51',object_id='https://bridge.test/actors/users/alice/objects/comments/51',kind='comment',body_markdown='reply',in_reply_to_object_id='https://bridge.test/actors/users/alice/objects/posts/50',discord_channel_id=902,discord_message_id=5001),
        m.MessageMapping(id=52,source_platform='discord',source_id='opaque-source',activity_id='https://bridge.test/actors/users/alice/activities/create/posts/52',object_id='https://bridge.test/actors/users/alice/objects/posts/52',actor_url='https://bridge.test/actors/users/alice',community_actor_url='https://remote.test/c/news',discord_channel_id=902,discord_message_id=5002),
        m.PostLink(id=60,lemmy_post_id=60,discord_forum_channel_id=902,discord_forum_thread_id=8000,discord_starter_message_id=8001,direction='inbound'),
        m.CommentLink(id=61,lemmy_comment_id=61,lemmy_post_id=60,discord_forum_thread_id=8000,discord_message_id=8002,direction='inbound'),
        m.DiscordGuildSnapshot(id=65,discord_guild_id=1,guild_name='Guild test'),
        m.DiscordChannelSnapshot(id=66,discord_channel_id=902,discord_guild_id=1,channel_name='Remote forum',channel_type='forum'),
        m.DiscordChannelSnapshot(id=67,discord_channel_id=900,discord_guild_id=1,channel_name='Local forum',channel_type='forum'),
        m.ActivityPubEventReceipt(id=70,delivery_id='delivery-70',event_type='post.created',object_ap_id='https://remote.test/o/70',status='processed'),
        m.ManagementAuditEvent(id=71,action='community.created',result='success',target_type='community',target_id='10',actor_discord_user_id='1001',after_json='{"audit":true}'),
    ])
    s.commit()
with Session(db.engine) as s:
    # These publications existed before upgrade; incoming Updates cannot supply them.
    for model, ident, kind, community, container, message in [
        (m.CommunityThreadGroup, 30, 'post', 'https://remote.test/c/news', 1000, 1000),
        (m.CommunityMessageGroup, 33, 'comment', 'https://remote.test/c/news', 1000, 1002),
        (m.LocalCommunityThread, 40, 'post', 'https://bridge.test/actors/communities/news', 3000, 3001),
        (m.LocalCommunityMessage, 43, 'comment', 'https://bridge.test/actors/communities/news', 3000, 3002),
        (m.LocalCommunityMessage, 44, 'comment', 'https://bridge.test/actors/communities/news', 3100, 3103),
    ]:
        row = s.get(model, ident)
        s.add(m.PublishedActivityObject(
            actor_username='alice', actor_url='https://bridge.test/actors/users/alice',
            community_actor_url=community, activity_id=row.ap_activity_id,
            object_id=row.ap_object_id, kind=kind, body_markdown='Original body',
            discord_channel_id=container, discord_message_id=message))
    s.commit()
with Session(db.engine) as s:
    # A nested reply authored in a subscriber forum also has a host bot copy.
    s.add(m.LocalCommunityMessageSurface(id=48, local_community_message_id=44,
        local_community_thread_surface_id=42, discord_forum_channel_id=901,
        discord_message_id=3103, parent_discord_message_id=3102,
        local_subscriber_id=11, role='local_subscriber'))
    # Inbound objects retain independent original-author evidence in the relay ledger.
    for ident, kind in [(60, 'post'), (61, 'comment')]:
        object_id = f'https://remote.test/legacy/{ident}'
        activity_id = f'https://remote.test/legacy/create/{ident}'
        actor = 'https://remote.test/u/original'
        if kind == 'post':
            s.add(m.LocalCommunityThread(id=ident, local_community_id=10,
                ap_activity_id=activity_id, ap_object_id=object_id,
                direction='inbound', origin_kind='remote_follower'))
        else:
            s.add(m.LocalCommunityMessage(id=ident, local_community_thread_id=60,
                ap_activity_id=activity_id, ap_object_id=object_id,
                parent_ap_object_id='https://remote.test/legacy/60', direction='inbound'))
        s.add(m.LocalCommunityRelaySourceActivity(local_community_id=10,
            object_kind=kind, operation='create', source_object_ap_id=object_id,
            source_activity_id=activity_id, origin_remote_actor_id=actor,
            source_activity_json={'id': activity_id, 'type': 'Create', 'actor': actor,
                'object': {'id': object_id, 'type': 'Note', 'attributedTo': actor,
                           'content': 'Original inbound body'}}))
    s.commit()
with Session(db.engine) as s:
    # The v0 post records subscriber origin, but not a trustworthy source receipt.
    object_id = 'https://bridge.test/actors/users/alice/objects/posts/62'
    activity_id = 'https://bridge.test/actors/users/alice/activities/create/posts/62'
    s.add(m.LocalCommunityThread(id=62, local_community_id=10,
        ap_activity_id=activity_id, ap_object_id=object_id,
        direction='outbound', origin_kind='local_subscriber'))
    s.add_all([
        m.LocalCommunityThreadSurface(id=63, local_community_thread_id=62,
            discord_forum_channel_id=900, discord_thread_id=3200,
            discord_starter_message_id=3201, role='host'),
        m.LocalCommunityThreadSurface(id=64, local_community_thread_id=62,
            discord_forum_channel_id=901, discord_thread_id=3300,
            discord_starter_message_id=3301, role='local_subscriber', local_subscriber_id=11),
        m.PublishedActivityObject(actor_username='alice',
            actor_url='https://bridge.test/actors/users/alice',
            community_actor_url='https://bridge.test/actors/communities/news',
            activity_id=activity_id, object_id=object_id, kind='post',
            body_markdown='subscriber source', discord_channel_id=3300, discord_message_id=3301),
    ])
    s.commit()
with db.engine.connect() as c:
    print('TAG-SEED OK', 'table_count:',len(m.Base.metadata.tables),'rows:',sum(c.exec_driver_sql(f'SELECT COUNT(*) FROM "{t}"').scalar_one() for t in m.Base.metadata.tables), 'schema_version:',c.exec_driver_sql('PRAGMA user_version').scalar_one())
    print('FK:',c.exec_driver_sql('PRAGMA foreign_key_check').all(),'integrity:',c.exec_driver_sql('PRAGMA integrity_check').scalar_one())
db.engine.dispose()
