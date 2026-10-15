-- Immutable v0.1.0 (8ab4403e): real tagged Database.create_all()/migrate().
CREATE TABLE activitypub_event_receipts (
	id INTEGER NOT NULL,
	delivery_id VARCHAR(128) NOT NULL,
	event_type VARCHAR(64) NOT NULL,
	object_ap_id VARCHAR(512) NOT NULL,
	status VARCHAR(32) NOT NULL,
	outcome VARCHAR(64),
	detail VARCHAR(1024),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (delivery_id)
);
CREATE TABLE bridge_actor_follows (
	id INTEGER NOT NULL,
	community_actor_id VARCHAR(512) NOT NULL,
	follow_activity_id VARCHAR(512),
	community_inbox_url VARCHAR(512),
	status VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (community_actor_id)
);
CREATE TABLE bridge_actor_keys (
	id INTEGER NOT NULL,
	actor_url VARCHAR(512) NOT NULL,
	key_id VARCHAR(768) NOT NULL,
	key_format VARCHAR(16) NOT NULL,
	algorithm VARCHAR(64) NOT NULL,
	public_key_data TEXT NOT NULL,
	private_key_data TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (actor_url),
	UNIQUE (key_id),
	CONSTRAINT ck_bridge_actor_keys_singleton CHECK (id = 1)
);
CREATE TABLE bridge_policy_entries (
	id INTEGER NOT NULL,
	policy_type VARCHAR(64) NOT NULL,
	normalized_subject VARCHAR(512) NOT NULL,
	status VARCHAR(32) NOT NULL,
	reason VARCHAR(500),
	created_by_discord_user_id VARCHAR(64) NOT NULL,
	updated_by_discord_user_id VARCHAR(64) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (policy_type, normalized_subject)
);
CREATE TABLE channel_community_subscriptions (
	id INTEGER NOT NULL,
	discord_channel_id INTEGER NOT NULL,
	discord_guild_id INTEGER,
	lemmy_community_actor_id VARCHAR(512) NOT NULL,
	lemmy_community_name VARCHAR(255),
	lemmy_community_id INTEGER,
	community_handle VARCHAR(255),
	community_inbox_url VARCHAR(512),
	follow_activity_id VARCHAR(512),
	initiated_by_discord_user_id VARCHAR(64),
	status VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (discord_channel_id)
);
CREATE TABLE comment_links (
	id INTEGER NOT NULL,
	lemmy_comment_id INTEGER NOT NULL,
	lemmy_comment_ap_id VARCHAR(512),
	lemmy_parent_comment_ap_id VARCHAR(512),
	lemmy_post_id INTEGER NOT NULL,
	discord_forum_thread_id INTEGER NOT NULL,
	discord_message_id INTEGER NOT NULL,
	direction VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (lemmy_comment_id, discord_forum_thread_id),
	UNIQUE (discord_message_id)
);
CREATE TABLE community_actor_bans (
	id INTEGER NOT NULL,
	scope VARCHAR(32) NOT NULL,
	scope_key VARCHAR(64),
	local_community_id INTEGER,
	actor_handle VARCHAR(255) NOT NULL,
	actor_url VARCHAR(512),
	target_discord_user_id VARCHAR(64),
	status VARCHAR(32) NOT NULL,
	created_by_discord_user_id VARCHAR(64),
	reason VARCHAR(1024),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (scope, scope_key, actor_handle, status)
);
CREATE TABLE community_message_group_deliveries (
	id INTEGER NOT NULL,
	message_group_id INTEGER NOT NULL,
	discord_channel_id INTEGER NOT NULL,
	discord_thread_id INTEGER NOT NULL,
	discord_message_id INTEGER NOT NULL,
	role VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (message_group_id, discord_channel_id),
	UNIQUE (discord_message_id)
);
CREATE TABLE community_message_groups (
	id INTEGER NOT NULL,
	community_actor_id VARCHAR(512) NOT NULL,
	thread_group_id INTEGER NOT NULL,
	source_channel_id INTEGER,
	source_thread_id INTEGER,
	source_message_id INTEGER,
	ap_activity_id VARCHAR(512),
	ap_object_id VARCHAR(512),
	parent_message_group_id INTEGER,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE community_thread_group_deliveries (
	id INTEGER NOT NULL,
	thread_group_id INTEGER NOT NULL,
	discord_channel_id INTEGER NOT NULL,
	discord_thread_id INTEGER NOT NULL,
	discord_starter_message_id INTEGER NOT NULL,
	role VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (thread_group_id, discord_channel_id),
	UNIQUE (discord_thread_id)
);
CREATE TABLE community_thread_groups (
	id INTEGER NOT NULL,
	community_actor_id VARCHAR(512) NOT NULL,
	source_channel_id INTEGER,
	source_thread_id INTEGER,
	source_starter_message_id INTEGER,
	ap_activity_id VARCHAR(512),
	ap_object_id VARCHAR(512),
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_thread_id)
);
CREATE TABLE discord_channel_snapshots (
	id INTEGER NOT NULL,
	discord_channel_id INTEGER NOT NULL,
	discord_guild_id INTEGER,
	channel_name VARCHAR(255) NOT NULL,
	channel_type VARCHAR(64) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (discord_channel_id)
);
CREATE TABLE discord_guild_snapshots (
	id INTEGER NOT NULL,
	discord_guild_id INTEGER NOT NULL,
	guild_name VARCHAR(255) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (discord_guild_id)
);
CREATE TABLE guild_invite_publications (
	id INTEGER NOT NULL,
	discord_guild_id INTEGER NOT NULL,
	discord_channel_id INTEGER NOT NULL,
	invite_code VARCHAR(128) NOT NULL,
	invite_url VARCHAR(512) NOT NULL,
	published_by_discord_user_id VARCHAR(64) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (discord_guild_id)
);
CREATE TABLE local_communities (
	id INTEGER NOT NULL,
	discord_guild_id INTEGER NOT NULL,
	discord_forum_channel_id INTEGER NOT NULL,
	slug VARCHAR(255) NOT NULL,
	display_name VARCHAR(255) NOT NULL,
	summary VARCHAR,
	created_by_discord_user_id VARCHAR(64),
	actor_url VARCHAR(512) NOT NULL,
	inbox_url VARCHAR(512) NOT NULL,
	outbox_url VARCHAR(512) NOT NULL,
	followers_url VARCHAR(512) NOT NULL,
	public_key_pem VARCHAR NOT NULL,
	private_key_pem VARCHAR NOT NULL,
	status VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (discord_forum_channel_id),
	UNIQUE (slug),
	UNIQUE (actor_url)
);
CREATE TABLE local_community_message_surfaces (
	id INTEGER NOT NULL,
	local_community_message_id INTEGER NOT NULL,
	local_community_thread_surface_id INTEGER NOT NULL,
	discord_forum_channel_id INTEGER NOT NULL,
	discord_message_id INTEGER NOT NULL,
	parent_discord_message_id INTEGER,
	local_subscriber_id INTEGER,
	role VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (local_community_message_id, local_community_thread_surface_id),
	UNIQUE (discord_message_id)
);
CREATE TABLE local_community_messages (
	id INTEGER NOT NULL,
	local_community_thread_id INTEGER NOT NULL,
	ap_activity_id VARCHAR(512) NOT NULL,
	ap_object_id VARCHAR(512) NOT NULL,
	parent_ap_object_id VARCHAR(512),
	direction VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (ap_activity_id),
	UNIQUE (ap_object_id)
);
CREATE TABLE local_community_relay_deliveries (
	id INTEGER NOT NULL,
	source_activity_row_id INTEGER NOT NULL,
	local_community_id INTEGER NOT NULL,
	object_kind VARCHAR(32) NOT NULL,
	operation VARCHAR(32) NOT NULL,
	source_object_ap_id VARCHAR(512) NOT NULL,
	source_activity_id VARCHAR(512) NOT NULL,
	origin_remote_actor_id VARCHAR(512) NOT NULL,
	target_remote_actor_id VARCHAR(512) NOT NULL,
	target_inbox_url VARCHAR(512) NOT NULL,
	delivery_profile VARCHAR(64) NOT NULL,
	relay_activity_id VARCHAR(512),
	status VARCHAR(32) NOT NULL,
	attempt_count INTEGER NOT NULL,
	last_attempted_at DATETIME,
	last_error VARCHAR(1024),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_activity_row_id, target_remote_actor_id)
);
CREATE TABLE local_community_relay_source_activities (
	id INTEGER NOT NULL,
	local_community_id INTEGER NOT NULL,
	object_kind VARCHAR(32) NOT NULL,
	operation VARCHAR(32) NOT NULL,
	source_object_ap_id VARCHAR(512) NOT NULL,
	source_activity_id VARCHAR(512) NOT NULL,
	source_announce_id VARCHAR(512),
	origin_remote_actor_id VARCHAR(512) NOT NULL,
	source_activity_json JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (local_community_id, operation, source_object_ap_id, source_activity_id)
);
CREATE TABLE local_community_thread_surfaces (
	id INTEGER NOT NULL,
	local_community_thread_id INTEGER NOT NULL,
	discord_forum_channel_id INTEGER NOT NULL,
	discord_thread_id INTEGER NOT NULL,
	discord_starter_message_id INTEGER NOT NULL,
	local_subscriber_id INTEGER,
	role VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (local_community_thread_id, discord_forum_channel_id),
	UNIQUE (discord_thread_id),
	UNIQUE (discord_starter_message_id)
);
CREATE TABLE local_community_threads (
	id INTEGER NOT NULL,
	local_community_id INTEGER NOT NULL,
	ap_activity_id VARCHAR(512) NOT NULL,
	ap_object_id VARCHAR(512) NOT NULL,
	direction VARCHAR(32) NOT NULL,
	origin_kind VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (ap_activity_id),
	UNIQUE (ap_object_id)
);
CREATE TABLE local_subscribers (
	id INTEGER NOT NULL,
	local_community_id INTEGER NOT NULL,
	discord_guild_id INTEGER,
	discord_channel_id INTEGER NOT NULL,
	initiated_by_discord_user_id VARCHAR(64),
	status VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (local_community_id, discord_channel_id),
	UNIQUE (discord_channel_id)
);
CREATE TABLE management_audit_events (
	id INTEGER NOT NULL,
	action VARCHAR(64) NOT NULL,
	result VARCHAR(32) NOT NULL,
	reason_code VARCHAR(64),
	actor_discord_user_id VARCHAR(64) NOT NULL,
	local_community_id INTEGER,
	target_type VARCHAR(64) NOT NULL,
	target_id VARCHAR(512),
	before_json TEXT,
	after_json TEXT,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE message_mappings (
	id INTEGER NOT NULL,
	source_platform VARCHAR(32) NOT NULL,
	source_id VARCHAR(255) NOT NULL,
	activity_id VARCHAR(512) NOT NULL,
	object_id VARCHAR(512) NOT NULL,
	actor_url VARCHAR(512) NOT NULL,
	community_actor_url VARCHAR(512) NOT NULL,
	discord_channel_id INTEGER,
	discord_message_id INTEGER,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_platform, source_id),
	UNIQUE (activity_id),
	UNIQUE (object_id),
	UNIQUE (discord_message_id)
);
CREATE TABLE post_links (
	id INTEGER NOT NULL,
	lemmy_post_id INTEGER NOT NULL,
	lemmy_post_ap_id VARCHAR(512),
	discord_forum_channel_id INTEGER,
	discord_forum_thread_id INTEGER NOT NULL,
	discord_starter_message_id INTEGER,
	direction VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (lemmy_post_id, discord_forum_channel_id),
	UNIQUE (discord_forum_thread_id)
);
CREATE TABLE published_activity_objects (
	id INTEGER NOT NULL,
	actor_username VARCHAR(255) NOT NULL,
	actor_url VARCHAR(512) NOT NULL,
	community_actor_url VARCHAR(512) NOT NULL,
	activity_id VARCHAR(512) NOT NULL,
	object_id VARCHAR(512) NOT NULL,
	kind VARCHAR(32) NOT NULL,
	title VARCHAR(512),
	body_markdown VARCHAR NOT NULL,
	in_reply_to_object_id VARCHAR(512),
	discord_channel_id INTEGER,
	discord_message_id INTEGER,
	published_at DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (activity_id),
	UNIQUE (object_id)
);
CREATE TABLE registration_sessions (
	id INTEGER NOT NULL,
	session_token VARCHAR(255) NOT NULL,
	oauth_state VARCHAR(255),
	discord_user_id VARCHAR(64),
	discord_username VARCHAR(255),
	discord_avatar_url VARCHAR(512),
	activitypub_username VARCHAR(255),
	status VARCHAR(32) NOT NULL,
	expires_at DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (session_token)
);
CREATE TABLE remote_actors (
	id INTEGER NOT NULL,
	actor_url VARCHAR(512) NOT NULL,
	preferred_username VARCHAR(255),
	inbox_url VARCHAR(512) NOT NULL,
	shared_inbox_url VARCHAR(512),
	public_key_pem VARCHAR NOT NULL,
	last_fetched_at DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (actor_url)
);
CREATE TABLE remote_subscribers (
	id INTEGER NOT NULL,
	local_community_id INTEGER NOT NULL,
	remote_actor_id VARCHAR(512) NOT NULL,
	remote_inbox_url VARCHAR(512) NOT NULL,
	follow_activity_id VARCHAR(512) NOT NULL,
	status VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (local_community_id, remote_actor_id),
	UNIQUE (follow_activity_id)
);
CREATE TABLE users (
	id INTEGER NOT NULL,
	discord_user_id VARCHAR(64) NOT NULL,
	activitypub_username VARCHAR(255) NOT NULL,
	actor_url VARCHAR(512) NOT NULL,
	inbox_url VARCHAR(512) NOT NULL,
	outbox_url VARCHAR(512) NOT NULL,
	followers_url VARCHAR(512) NOT NULL,
	public_key_pem VARCHAR NOT NULL,
	private_key_pem VARCHAR NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (discord_user_id),
	UNIQUE (activitypub_username),
	UNIQUE (actor_url)
);
CREATE INDEX ix_bridge_policy_entries_normalized_subject ON bridge_policy_entries (normalized_subject);
CREATE INDEX ix_bridge_policy_entries_policy_type ON bridge_policy_entries (policy_type);
CREATE INDEX ix_bridge_policy_entries_status ON bridge_policy_entries (status);
CREATE INDEX ix_community_actor_bans_target_discord_user_id ON community_actor_bans (target_discord_user_id);
CREATE INDEX ix_management_audit_events_action ON management_audit_events (action);
CREATE INDEX ix_management_audit_events_actor_discord_user_id ON management_audit_events (actor_discord_user_id);
CREATE INDEX ix_management_audit_events_created_at ON management_audit_events (created_at);
CREATE INDEX ix_management_audit_events_local_community_id ON management_audit_events (local_community_id);
CREATE INDEX ix_management_audit_events_reason_code ON management_audit_events (reason_code);
CREATE INDEX ix_management_audit_events_result ON management_audit_events (result);
CREATE INDEX ix_management_audit_events_target_id ON management_audit_events (target_id);
CREATE INDEX ix_management_audit_events_target_type ON management_audit_events (target_type);
