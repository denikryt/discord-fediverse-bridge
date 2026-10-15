"""Fixed SQL operations for the single released-v0 migration.

Statements describe known source tables explicitly. The runner executes each
statement on its existing transaction; this module has no connection or commit.
"""

COPY_SQL = {
    # The release had no profile/media fields. Initialize only new fields; preserve
    # signing material and original timestamps verbatim.
    "users": """
INSERT INTO main.users (
    id, discord_user_id, activitypub_username, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, display_name, summary, avatar_media_id, header_media_id, profile_version, created_at, updated_at
)
SELECT
    l.id,
    l.discord_user_id,
    l.activitypub_username,
    l.actor_url,
    l.inbox_url,
    l.outbox_url,
    l.followers_url,
    l.public_key_pem,
    l.private_key_pem,
    NULL,
    NULL,
    NULL,
    NULL,
    0,
    l.created_at,
    l.updated_at
FROM temp.__legacy_users l

""",
    # A known Discord origin keeps connector-scoped dedup even when no channel is known.
    # LEFT JOIN preserves that row; remote origins do not acquire a Discord identity.
    "message_mappings": """
INSERT INTO main.message_mappings (
    source_connector_id, source_channel_surface_id, id, source_platform, source_id, activity_id, object_id, actor_url, publication_context, community_actor_url, discord_guild_id, discord_channel_id, discord_message_id, created_at
)
SELECT
    CASE WHEN l.source_platform='discord' THEN 1 END,
    CASE WHEN l.source_platform='discord' THEN s.id END,
    l.id,
    l.source_platform,
    l.source_id,
    l.activity_id,
    l.object_id,
    l.actor_url,
    'community',
    l.community_actor_url,
    NULL,
    l.discord_channel_id,
    l.discord_message_id,
    l.created_at
FROM temp.__legacy_message_mappings l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_channel_id AS TEXT)
""",
    # Released publications belong to community context. Add source placement only from
    # persisted Discord evidence; incoming AP rows must not invent a physical source.
    "published_activity_objects": """
INSERT INTO main.published_activity_objects (
    source_connector_id, source_channel_surface_id, source_external_message_id, id, actor_username, actor_url, publication_context, community_actor_url, activity_id, object_id, kind, title, body_markdown, in_reply_to_object_id, conversation_root_object_id, discord_guild_id, discord_channel_id, discord_message_id, content_revision, deleted_at, desired_kind, desired_title, desired_body_markdown, desired_edited_timestamp, desired_fingerprint, discord_reconciliation_needed, discord_reconciliation_attempt_count, discord_reconciliation_next_attempt_at, discord_reconciliation_last_error, published_at, created_at
)
SELECT
    CASE WHEN l.discord_channel_id IS NOT NULL OR l.discord_message_id IS NOT NULL THEN 1 END,
    s.id,
    CAST(l.discord_message_id AS TEXT),
    l.id,
    l.actor_username,
    l.actor_url,
    'community',
    l.community_actor_url,
    l.activity_id,
    l.object_id,
    l.kind,
    l.title,
    l.body_markdown,
    l.in_reply_to_object_id,
    CASE WHEN l.kind='post' THEN l.object_id ELSE COALESCE(l.in_reply_to_object_id,l.object_id) END,
    NULL,
    l.discord_channel_id,
    l.discord_message_id,
    1,
    NULL,
    'none',
    NULL,
    NULL,
    NULL,
    NULL,
    0,
    0,
    NULL,
    NULL,
    l.published_at,
    l.created_at
FROM temp.__legacy_published_activity_objects l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_channel_id AS TEXT)
""",
    # Canonical AP identity is rewritten later by the validated cutover plan. This copy
    # only scopes physical source placement when it exists.
    "community_thread_groups": """
INSERT INTO main.community_thread_groups (
    source_connector_id, source_channel_surface_id, source_external_thread_id, id, community_actor_id, source_channel_id, source_thread_id, source_starter_message_id, ap_activity_id, ap_object_id, created_at
)
SELECT
    CASE WHEN s.id IS NOT NULL THEN 1 END,
    s.id,
    CAST(l.source_thread_id AS TEXT),
    l.id,
    l.community_actor_id,
    l.source_channel_id,
    l.source_thread_id,
    l.source_starter_message_id,
    l.ap_activity_id,
    l.ap_object_id,
    l.created_at
FROM temp.__legacy_community_thread_groups l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.source_channel_id AS TEXT)
""",
    # Source message identity is preserved independently of target copies; a missing
    # physical origin remains NULL rather than selecting a mirror.
    "community_message_groups": """
INSERT INTO main.community_message_groups (
    source_connector_id, source_channel_surface_id, source_external_message_id, id, community_actor_id, thread_group_id, source_channel_id, source_thread_id, source_message_id, ap_activity_id, ap_object_id, parent_message_group_id, created_at
)
SELECT
    CASE WHEN s.id IS NOT NULL THEN 1 END,
    s.id,
    CAST(l.source_message_id AS TEXT),
    l.id,
    l.community_actor_id,
    l.thread_group_id,
    l.source_channel_id,
    l.source_thread_id,
    l.source_message_id,
    l.ap_activity_id,
    l.ap_object_id,
    l.parent_message_group_id,
    l.created_at
FROM temp.__legacy_community_message_groups l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.source_channel_id AS TEXT)
""",
    # The actor retains its keys and content identity. Legacy Discord host fields move to
    # the typed ChannelSurface claim, not to a second actor location.
    "local_communities": """
INSERT INTO main.local_communities (
    id, slug, display_name, summary, created_by_discord_user_id, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, status, created_at, updated_at
)
SELECT
    l.id,
    l.slug,
    l.display_name,
    l.summary,
    l.created_by_discord_user_id,
    l.actor_url,
    l.inbox_url,
    l.outbox_url,
    l.followers_url,
    l.public_key_pem,
    l.private_key_pem,
    l.status,
    l.created_at,
    l.updated_at
FROM temp.__legacy_local_communities l

""",
    # Thread placement is tied to the persisted forum copy. This compatibility mapping
    # must not create a new authored publication.
    "post_links": """
INSERT INTO main.post_links (
    connector_id, channel_surface_id, external_thread_id, external_starter_message_id, id, lemmy_post_id, lemmy_post_ap_id, discord_forum_channel_id, discord_forum_thread_id, discord_starter_message_id, direction, created_at
)
SELECT
    1,
    s.id,
    CAST(l.discord_forum_thread_id AS TEXT),
    CAST(l.discord_starter_message_id AS TEXT),
    l.id,
    l.lemmy_post_id,
    l.lemmy_post_ap_id,
    l.discord_forum_channel_id,
    l.discord_forum_thread_id,
    l.discord_starter_message_id,
    l.direction,
    l.created_at
FROM temp.__legacy_post_links l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_forum_channel_id AS TEXT)
""",
    # The release did not store an external parent message ID. Keep it NULL rather than
    # guessing a parent from another thread or copy.
    "comment_links": """
INSERT INTO main.comment_links (
    connector_id, channel_surface_id, external_thread_id, external_message_id, external_parent_message_id, id, lemmy_comment_id, lemmy_comment_ap_id, lemmy_parent_comment_ap_id, lemmy_post_id, discord_forum_thread_id, discord_message_id, direction, created_at
)
SELECT
    1,
    s.id,
    CAST(l.discord_forum_thread_id AS TEXT),
    CAST(l.discord_message_id AS TEXT),
    NULL,
    l.id,
    l.lemmy_comment_id,
    l.lemmy_comment_ap_id,
    l.lemmy_parent_comment_ap_id,
    l.lemmy_post_id,
    l.discord_forum_thread_id,
    l.discord_message_id,
    l.direction,
    l.created_at
FROM temp.__legacy_comment_links l
LEFT JOIN temp.__thread_locations t ON t.thread_id=l.discord_forum_thread_id
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(t.channel_id AS TEXT)
""",
    # Each historical target keeps its own starter/thread identity under the physical
    # channel connector.
    "community_thread_group_deliveries": """
INSERT INTO main.community_thread_group_deliveries (
    connector_id, channel_surface_id, external_thread_id, external_starter_message_id, id, thread_group_id, discord_channel_id, discord_thread_id, discord_starter_message_id, role
)
SELECT
    1,
    s.id,
    CAST(l.discord_thread_id AS TEXT),
    CAST(l.discord_starter_message_id AS TEXT),
    l.id,
    l.thread_group_id,
    l.discord_channel_id,
    l.discord_thread_id,
    l.discord_starter_message_id,
    l.role
FROM temp.__legacy_community_thread_group_deliveries l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_channel_id AS TEXT)
""",
    # Resolve replies only against the parent delivery in this same channel and thread. A
    # parent in another mirror cannot be used as a reply target.
    "community_message_group_deliveries": """
INSERT INTO main.community_message_group_deliveries (
    connector_id, channel_surface_id, external_thread_id, external_message_id, external_parent_message_id, id, message_group_id, discord_channel_id, discord_thread_id, discord_message_id, role
)
SELECT
    1,
    s.id,
    CAST(l.discord_thread_id AS TEXT),
    CAST(l.discord_message_id AS TEXT),
    CAST(p.discord_message_id AS TEXT),
    l.id,
    l.message_group_id,
    l.discord_channel_id,
    l.discord_thread_id,
    l.discord_message_id,
    l.role
FROM temp.__legacy_community_message_group_deliveries l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_channel_id AS TEXT)
JOIN temp.__legacy_community_message_groups g ON g.id=l.message_group_id
LEFT JOIN temp.__legacy_community_message_group_deliveries p ON p.message_group_id=g.parent_message_group_id AND p.discord_channel_id=l.discord_channel_id AND p.discord_thread_id=l.discord_thread_id
""",
    # Scope each host/subscriber copy to its physical channel; the actor and subscriber
    # remain separate owners.
    "local_community_thread_surfaces": """
INSERT INTO main.local_community_thread_surfaces (
    connector_id, channel_surface_id, external_thread_id, external_starter_message_id, id, local_community_thread_id, discord_forum_channel_id, discord_thread_id, discord_starter_message_id, local_subscriber_id, role, created_at
)
SELECT
    1,
    s.id,
    CAST(l.discord_thread_id AS TEXT),
    CAST(l.discord_starter_message_id AS TEXT),
    l.id,
    l.local_community_thread_id,
    l.discord_forum_channel_id,
    l.discord_thread_id,
    l.discord_starter_message_id,
    l.local_subscriber_id,
    l.role,
    l.created_at
FROM temp.__legacy_local_community_thread_surfaces l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_forum_channel_id AS TEXT)
""",
    # Preserve the exact containing thread surface. Joining only by actor or message
    # would permit cross-channel replies.
    "local_community_message_surfaces": """
INSERT INTO main.local_community_message_surfaces (
    connector_id, channel_surface_id, external_message_id, external_parent_message_id, id, local_community_message_id, local_community_thread_surface_id, discord_forum_channel_id, discord_message_id, parent_discord_message_id, local_subscriber_id, role, created_at
)
SELECT
    1,
    s.id,
    CAST(l.discord_message_id AS TEXT),
    CAST(l.parent_discord_message_id AS TEXT),
    l.id,
    l.local_community_message_id,
    l.local_community_thread_surface_id,
    l.discord_forum_channel_id,
    l.discord_message_id,
    l.parent_discord_message_id,
    l.local_subscriber_id,
    l.role,
    l.created_at
FROM temp.__legacy_local_community_message_surfaces l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_forum_channel_id AS TEXT)
""",
}

# Scalar links absent from released FK DDL require explicit relational checks.
RELATIONSHIP_CHECKS = {
    "Missing legacy parent": """
SELECT 'local_subscribers.local_community_id' AS relationship, child.id AS child_id, child.local_community_id AS parent_id
FROM main.local_subscribers AS child
LEFT JOIN main.local_communities AS parent ON parent.id=child.local_community_id
WHERE child.local_community_id IS NOT NULL AND parent.id IS NULL
UNION ALL
SELECT 'remote_subscribers.local_community_id' AS relationship, child.id AS child_id, child.local_community_id AS parent_id
FROM main.remote_subscribers AS child
LEFT JOIN main.local_communities AS parent ON parent.id=child.local_community_id
WHERE child.local_community_id IS NOT NULL AND parent.id IS NULL
UNION ALL
SELECT 'local_community_threads.local_community_id' AS relationship, child.id AS child_id, child.local_community_id AS parent_id
FROM main.local_community_threads AS child
LEFT JOIN main.local_communities AS parent ON parent.id=child.local_community_id
WHERE child.local_community_id IS NOT NULL AND parent.id IS NULL
UNION ALL
SELECT 'local_community_messages.local_community_thread_id' AS relationship, child.id AS child_id, child.local_community_thread_id AS parent_id
FROM main.local_community_messages AS child
LEFT JOIN main.local_community_threads AS parent ON parent.id=child.local_community_thread_id
WHERE child.local_community_thread_id IS NOT NULL AND parent.id IS NULL
UNION ALL
SELECT 'local_community_thread_surfaces.local_community_thread_id' AS relationship, child.id AS child_id, child.local_community_thread_id AS parent_id
FROM main.local_community_thread_surfaces AS child
LEFT JOIN main.local_community_threads AS parent ON parent.id=child.local_community_thread_id
WHERE child.local_community_thread_id IS NOT NULL AND parent.id IS NULL
UNION ALL
SELECT 'local_community_thread_surfaces.local_subscriber_id' AS relationship, child.id AS child_id, child.local_subscriber_id AS parent_id
FROM main.local_community_thread_surfaces AS child
LEFT JOIN main.local_subscribers AS parent ON parent.id=child.local_subscriber_id
WHERE child.local_subscriber_id IS NOT NULL AND parent.id IS NULL
UNION ALL
SELECT 'local_community_message_surfaces.local_community_message_id' AS relationship, child.id AS child_id, child.local_community_message_id AS parent_id
FROM main.local_community_message_surfaces AS child
LEFT JOIN main.local_community_messages AS parent ON parent.id=child.local_community_message_id
WHERE child.local_community_message_id IS NOT NULL AND parent.id IS NULL
UNION ALL
SELECT 'local_community_message_surfaces.local_community_thread_surface_id' AS relationship, child.id AS child_id, child.local_community_thread_surface_id AS parent_id
FROM main.local_community_message_surfaces AS child
LEFT JOIN main.local_community_thread_surfaces AS parent ON parent.id=child.local_community_thread_surface_id
WHERE child.local_community_thread_surface_id IS NOT NULL AND parent.id IS NULL
UNION ALL
SELECT 'community_message_groups.thread_group_id' AS relationship, child.id AS child_id, child.thread_group_id AS parent_id
FROM main.community_message_groups AS child
LEFT JOIN main.community_thread_groups AS parent ON parent.id=child.thread_group_id
WHERE child.thread_group_id IS NOT NULL AND parent.id IS NULL
UNION ALL
SELECT 'community_message_groups.parent_message_group_id' AS relationship, child.id AS child_id, child.parent_message_group_id AS parent_id
FROM main.community_message_groups AS child
LEFT JOIN main.community_message_groups AS parent ON parent.id=child.parent_message_group_id
WHERE child.parent_message_group_id IS NOT NULL AND parent.id IS NULL
UNION ALL
SELECT 'community_thread_group_deliveries.thread_group_id' AS relationship, child.id AS child_id, child.thread_group_id AS parent_id
FROM main.community_thread_group_deliveries AS child
LEFT JOIN main.community_thread_groups AS parent ON parent.id=child.thread_group_id
WHERE child.thread_group_id IS NOT NULL AND parent.id IS NULL
UNION ALL
SELECT 'community_message_group_deliveries.message_group_id' AS relationship, child.id AS child_id, child.message_group_id AS parent_id
FROM main.community_message_group_deliveries AS child
LEFT JOIN main.community_message_groups AS parent ON parent.id=child.message_group_id
WHERE child.message_group_id IS NOT NULL AND parent.id IS NULL
LIMIT 1
""",
    # Never resolve competing owners by row order or a priority rule.
    "Conflicting legacy owners": """
SELECT channel AS channel_id, count(*) AS owner_count FROM (
 SELECT discord_forum_channel_id AS channel FROM main.local_communities
 UNION ALL SELECT discord_channel_id FROM main.local_subscribers
 UNION ALL SELECT discord_channel_id FROM main.channel_community_subscriptions
) GROUP BY channel HAVING count(*) > 1 LIMIT 1
""",
    "Conflicting legacy guild/channel kind": """
WITH evidence AS (SELECT discord_channel_id AS channel, discord_guild_id AS guild, 'forum' AS kind
FROM main.channel_community_subscriptions WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, NULL AS guild, 'forum' AS kind
FROM main.community_message_group_deliveries WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT source_channel_id AS channel, NULL AS guild, 'unknown' AS kind
FROM main.community_message_groups WHERE source_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, NULL AS guild, 'forum' AS kind
FROM main.community_thread_group_deliveries WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT source_channel_id AS channel, NULL AS guild, 'unknown' AS kind
FROM main.community_thread_groups WHERE source_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, discord_guild_id AS guild, CASE channel_type WHEN 'news' THEN 'announcement' WHEN 'announcement' THEN 'announcement' WHEN 'forum' THEN 'forum' WHEN 'text' THEN 'text' ELSE 'unknown' END AS kind
FROM main.discord_channel_snapshots WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, discord_guild_id AS guild, 'unknown' AS kind
FROM main.guild_invite_publications WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT discord_forum_channel_id AS channel, discord_guild_id AS guild, 'forum' AS kind
FROM main.local_communities WHERE discord_forum_channel_id IS NOT NULL
UNION ALL
SELECT discord_forum_channel_id AS channel, NULL AS guild, 'forum' AS kind
FROM main.local_community_message_surfaces WHERE discord_forum_channel_id IS NOT NULL
UNION ALL
SELECT discord_forum_channel_id AS channel, NULL AS guild, 'forum' AS kind
FROM main.local_community_thread_surfaces WHERE discord_forum_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, discord_guild_id AS guild, 'forum' AS kind
FROM main.local_subscribers WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, NULL AS guild, 'unknown' AS kind
FROM main.message_mappings WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT discord_forum_channel_id AS channel, NULL AS guild, 'forum' AS kind
FROM main.post_links WHERE discord_forum_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, NULL AS guild, 'unknown' AS kind
FROM main.published_activity_objects WHERE discord_channel_id IS NOT NULL)
SELECT channel AS channel_id, count(DISTINCT guild) AS guild_count, count(DISTINCT NULLIF(kind,'unknown')) AS kind_count FROM evidence GROUP BY channel
HAVING count(DISTINCT guild)>1 OR count(DISTINCT NULLIF(kind,'unknown'))>1 LIMIT 1
""",
    "Conflicting legacy parent thread": """
SELECT child.id AS message_group_id, parent.id AS parent_group_id, child.thread_group_id AS child_thread_id, parent.thread_group_id AS parent_thread_id FROM main.community_message_groups AS child
JOIN main.community_message_groups AS parent ON parent.id=child.parent_message_group_id
WHERE child.thread_group_id IS NOT parent.thread_group_id
 OR child.community_actor_id IS NOT parent.community_actor_id LIMIT 1
""",
    "Cyclic legacy parent": """
WITH RECURSIVE rooted(id) AS (
 SELECT id FROM main.community_message_groups
 WHERE parent_message_group_id IS NULL
 UNION
 SELECT g.id FROM main.community_message_groups AS g
 JOIN rooted AS parent ON parent.id=g.parent_message_group_id
)
SELECT g.id AS message_group_id, g.parent_message_group_id AS parent_group_id FROM main.community_message_groups AS g
LEFT JOIN rooted AS valid ON valid.id=g.id
WHERE valid.id IS NULL LIMIT 1
""",
    "Missing target-local parent": """
SELECT child.id AS delivery_id, message.parent_message_group_id AS parent_group_id, child.discord_channel_id AS channel_id, child.discord_thread_id AS thread_id FROM main.community_message_group_deliveries AS child
JOIN main.community_message_groups AS message ON message.id=child.message_group_id
LEFT JOIN main.community_message_group_deliveries AS parent
 ON parent.message_group_id=message.parent_message_group_id
 AND parent.discord_channel_id=child.discord_channel_id
 AND parent.discord_thread_id=child.discord_thread_id
WHERE message.parent_message_group_id IS NOT NULL AND parent.id IS NULL LIMIT 1
""",
    "Conflicting legacy message/thread channel": """
SELECT surface.id AS surface_id, surface.discord_forum_channel_id AS channel_id, thread.discord_forum_channel_id AS thread_channel_id, message.local_community_thread_id AS message_thread_id, thread.local_community_thread_id AS surface_thread_id FROM main.local_community_message_surfaces AS surface
JOIN main.local_community_messages AS message ON message.id=surface.local_community_message_id
JOIN main.local_community_thread_surfaces AS thread ON thread.id=surface.local_community_thread_surface_id
WHERE message.local_community_thread_id IS NOT thread.local_community_thread_id
 OR surface.discord_forum_channel_id IS NOT thread.discord_forum_channel_id LIMIT 1
""",
}

# All known physical sources, preserving guild/kind conflict evidence.
# MAX projects the sole known value only after ambiguity is rejected; counts
# prevent choosing an arbitrary guild/kind when multiple source records disagree.
CHANNEL_INVENTORY = """
CREATE TEMP TABLE __channel_inventory AS
WITH evidence AS (
SELECT discord_channel_id AS channel, discord_guild_id AS guild, 'forum' AS kind
FROM temp.__legacy_channel_community_subscriptions WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, NULL AS guild, 'forum' AS kind
FROM temp.__legacy_community_message_group_deliveries WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT source_channel_id AS channel, NULL AS guild, 'unknown' AS kind
FROM temp.__legacy_community_message_groups WHERE source_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, NULL AS guild, 'forum' AS kind
FROM temp.__legacy_community_thread_group_deliveries WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT source_channel_id AS channel, NULL AS guild, 'unknown' AS kind
FROM temp.__legacy_community_thread_groups WHERE source_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, discord_guild_id AS guild, CASE channel_type WHEN 'news' THEN 'announcement' WHEN 'announcement' THEN 'announcement' WHEN 'forum' THEN 'forum' WHEN 'text' THEN 'text' ELSE 'unknown' END AS kind
FROM temp.__legacy_discord_channel_snapshots WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, discord_guild_id AS guild, 'unknown' AS kind
FROM temp.__legacy_guild_invite_publications WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT discord_forum_channel_id AS channel, discord_guild_id AS guild, 'forum' AS kind
FROM temp.__legacy_local_communities WHERE discord_forum_channel_id IS NOT NULL
UNION ALL
SELECT discord_forum_channel_id AS channel, NULL AS guild, 'forum' AS kind
FROM temp.__legacy_local_community_message_surfaces WHERE discord_forum_channel_id IS NOT NULL
UNION ALL
SELECT discord_forum_channel_id AS channel, NULL AS guild, 'forum' AS kind
FROM temp.__legacy_local_community_thread_surfaces WHERE discord_forum_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, discord_guild_id AS guild, 'forum' AS kind
FROM temp.__legacy_local_subscribers WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, NULL AS guild, 'unknown' AS kind
FROM temp.__legacy_message_mappings WHERE discord_channel_id IS NOT NULL
UNION ALL
SELECT discord_forum_channel_id AS channel, NULL AS guild, 'forum' AS kind
FROM temp.__legacy_post_links WHERE discord_forum_channel_id IS NOT NULL
UNION ALL
SELECT discord_channel_id AS channel, NULL AS guild, 'unknown' AS kind
FROM temp.__legacy_published_activity_objects WHERE discord_channel_id IS NOT NULL
)
SELECT channel, MAX(guild) guild,
 COALESCE(MAX(NULLIF(kind,'unknown')),'unknown') kind,
 COUNT(DISTINCT guild) guild_count,
 COUNT(DISTINCT NULLIF(kind,'unknown')) kind_count
FROM evidence GROUP BY channel
"""
