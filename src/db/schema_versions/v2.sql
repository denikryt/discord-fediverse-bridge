-- Immutable SQLite v2 physical schema; validated by schema_entries_fingerprint.
CREATE TABLE activitypub_event_receipts (
	id INTEGER NOT NULL,
	delivery_id VARCHAR(128) NOT NULL,
	verified_destination_group_actor_id VARCHAR(512),
	event_type VARCHAR(64) NOT NULL,
	object_ap_id VARCHAR(512) NOT NULL,
	status VARCHAR(32) NOT NULL,
	outcome VARCHAR(64),
	detail VARCHAR(1024),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE bridge_actor_follows (
	id INTEGER NOT NULL,
	community_actor_id VARCHAR(512) NOT NULL,
	follow_activity_id VARCHAR(512),
	remote_software_profile VARCHAR(32) DEFAULT 'unknown' NOT NULL,
	follow_dispatch_state VARCHAR(32) DEFAULT 'prepared' NOT NULL,
	cancel_requested BOOLEAN DEFAULT '0' NOT NULL,
	community_inbox_url VARCHAR(512),
	status VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_bridge_follow_profile CHECK (remote_software_profile IN ('unknown','nodebb','threadiverse','bridge')),
	CONSTRAINT ck_bridge_follow_dispatch CHECK (follow_dispatch_state IN ('prepared','sending','sent','undo_sending')),
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
CREATE TABLE community_discord_target_claims (
	id INTEGER NOT NULL,
	thread_group_id INTEGER,
	message_group_id INTEGER,
	discord_channel_id INTEGER NOT NULL,
	kind VARCHAR(16) NOT NULL,
	status VARCHAR(16) NOT NULL,
	attempt_id VARCHAR(64),
	attempt_started_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT ck_discord_target_kind_fk CHECK ((thread_group_id IS NOT NULL AND message_group_id IS NULL AND kind = 'post') OR (thread_group_id IS NULL AND message_group_id IS NOT NULL AND kind = 'comment')),
	CONSTRAINT ck_discord_target_status CHECK (status IN ('claimed', 'failed', 'completed', 'unknown')),
	FOREIGN KEY(thread_group_id) REFERENCES community_thread_groups (id) ON DELETE CASCADE,
	FOREIGN KEY(message_group_id) REFERENCES community_message_groups (id) ON DELETE CASCADE
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
	source_owner_actor_id VARCHAR(512),
	inbound_create_snapshot_json JSON,
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
	source_owner_actor_id VARCHAR(512),
	inbound_create_snapshot_json JSON,
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
CREATE TABLE inbound_conversation_objects (
	id INTEGER NOT NULL,
	object_id VARCHAR(512) NOT NULL,
	publication_context VARCHAR(32) NOT NULL,
	activity_id VARCHAR(512),
	conversation_root_object_id VARCHAR(512) NOT NULL,
	parent_object_id VARCHAR(512),
	actor_id VARCHAR(512) NOT NULL,
	actor_name VARCHAR(255),
	content_markdown TEXT NOT NULL,
	source_activity_json TEXT,
	published_at DATETIME,
	updated_at_remote DATETIME,
	deleted_at DATETIME,
	revision_fingerprint VARCHAR(64) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (object_id),
	UNIQUE (activity_id)
);
CREATE TABLE inbound_discord_deliveries (
	id INTEGER NOT NULL,
	inbound_object_id INTEGER NOT NULL,
	root_published_object_id INTEGER NOT NULL,
	destination_channel_id INTEGER NOT NULL,
	destination_thread_id INTEGER,
	discord_message_id INTEGER,
	state VARCHAR(32) NOT NULL,
	attempt_count INTEGER NOT NULL,
	next_attempt_at DATETIME,
	last_error VARCHAR(512),
	last_applied_revision VARCHAR(64),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (inbound_object_id, root_published_object_id),
	UNIQUE (discord_message_id),
	CONSTRAINT ck_inbound_discord_delivery_state CHECK (state IN ('pending_parent','pending_thread','ready','dispatching','delivered','suppressed','failed')),
	CONSTRAINT ck_inbound_discord_attempt_count CHECK (attempt_count >= 0 AND attempt_count <= 3),
	FOREIGN KEY(inbound_object_id) REFERENCES inbound_conversation_objects (id) ON DELETE CASCADE,
	FOREIGN KEY(root_published_object_id) REFERENCES published_activity_objects (id) ON DELETE CASCADE
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
	local_community_id INTEGER NOT NULL,
	ap_activity_id VARCHAR(512) NOT NULL,
	ap_object_id VARCHAR(512) NOT NULL,
	parent_ap_object_id VARCHAR(512),
	source_owner_actor_id VARCHAR(512),
	origin_subscriber_actor_id VARCHAR(512),
	inbound_create_snapshot_json JSON,
	host_delivery_state VARCHAR(16),
	host_attempt_id VARCHAR(64),
	host_attempt_started_at DATETIME,
	direction VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (local_community_id, ap_activity_id),
	UNIQUE (local_community_id, ap_object_id),
	CONSTRAINT fk_local_message_thread_tenant FOREIGN KEY(local_community_thread_id, local_community_id) REFERENCES local_community_threads (id, local_community_id),
	CONSTRAINT ck_local_message_host_state CHECK (host_delivery_state IS NULL OR host_delivery_state IN ('reserved','sending','delivered','failed','unknown')),
	CONSTRAINT ck_local_message_host_direction CHECK (direction = 'ap_to_discord' OR host_delivery_state IS NULL)
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
	attempt_token VARCHAR(64),
	attempt_started_at DATETIME,
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
	canonical_relay_projection_json JSON,
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
	source_owner_actor_id VARCHAR(512),
	origin_subscriber_actor_id VARCHAR(512),
	inbound_create_snapshot_json JSON,
	host_delivery_state VARCHAR(16),
	host_attempt_id VARCHAR(64),
	host_attempt_started_at DATETIME,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (local_community_id, ap_activity_id),
	UNIQUE (local_community_id, ap_object_id),
	UNIQUE (id, local_community_id),
	CONSTRAINT ck_local_thread_host_state CHECK (host_delivery_state IS NULL OR host_delivery_state IN ('reserved','sending','delivered','failed','unknown')),
	CONSTRAINT ck_local_thread_host_direction CHECK (direction = 'ap_to_discord' OR host_delivery_state IS NULL)
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
	publication_context VARCHAR(32) NOT NULL,
	community_actor_url VARCHAR(512),
	discord_guild_id INTEGER,
	discord_channel_id INTEGER,
	discord_message_id INTEGER,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_platform, source_id),
	UNIQUE (activity_id),
	UNIQUE (object_id),
	UNIQUE (discord_message_id),
	CONSTRAINT ck_message_mapping_publication_context CHECK ((publication_context = 'community' AND community_actor_url IS NOT NULL) OR (publication_context IN ('personal_feed','remote_person_feed') AND community_actor_url IS NULL AND discord_guild_id IS NOT NULL AND discord_channel_id IS NOT NULL AND discord_message_id IS NOT NULL))
);
CREATE TABLE personal_feed_bindings (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	discord_guild_id INTEGER NOT NULL,
	discord_channel_id INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (user_id),
	UNIQUE (discord_guild_id),
	UNIQUE (discord_channel_id),
	CONSTRAINT ck_personal_feed_guild_positive CHECK (discord_guild_id > 0),
	CONSTRAINT ck_personal_feed_channel_positive CHECK (discord_channel_id > 0),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE TABLE personal_feed_thread_mappings (
	id INTEGER NOT NULL,
	root_published_object_id INTEGER NOT NULL,
	root_discord_channel_id INTEGER NOT NULL,
	root_discord_message_id INTEGER NOT NULL,
	thread_id INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (root_published_object_id),
	UNIQUE (thread_id),
	CONSTRAINT ck_personal_feed_thread_identity CHECK (thread_id = root_discord_message_id),
	FOREIGN KEY(root_published_object_id) REFERENCES published_activity_objects (id) ON DELETE CASCADE
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
CREATE TABLE profile_media_assets (
	id INTEGER NOT NULL,
	public_id VARCHAR(64) NOT NULL,
	storage_key VARCHAR(255) NOT NULL,
	media_kind VARCHAR(16) NOT NULL,
	content_type VARCHAR(64) NOT NULL,
	byte_size INTEGER NOT NULL,
	width INTEGER NOT NULL,
	height INTEGER NOT NULL,
	sha256_hex VARCHAR(64) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (public_id),
	UNIQUE (storage_key),
	CONSTRAINT ck_profile_media_kind CHECK (media_kind IN ('avatar', 'header')),
	CONSTRAINT ck_profile_media_byte_size CHECK (byte_size > 0),
	CONSTRAINT ck_profile_media_dimensions CHECK (width > 0 AND height > 0)
);
CREATE TABLE published_activity_objects (
	id INTEGER NOT NULL,
	actor_username VARCHAR(255) NOT NULL,
	actor_url VARCHAR(512) NOT NULL,
	publication_context VARCHAR(32) NOT NULL,
	community_actor_url VARCHAR(512),
	activity_id VARCHAR(512) NOT NULL,
	object_id VARCHAR(512) NOT NULL,
	kind VARCHAR(32) NOT NULL,
	title VARCHAR(512),
	body_markdown VARCHAR NOT NULL,
	in_reply_to_object_id VARCHAR(512),
	conversation_root_object_id VARCHAR(512),
	discord_guild_id INTEGER,
	discord_channel_id INTEGER,
	discord_message_id INTEGER,
	content_revision INTEGER NOT NULL,
	deleted_at DATETIME,
	desired_kind VARCHAR(16) NOT NULL,
	desired_title VARCHAR(512),
	desired_body_markdown VARCHAR,
	desired_edited_timestamp DATETIME,
	desired_fingerprint VARCHAR(64),
	discord_reconciliation_needed BOOLEAN NOT NULL,
	discord_reconciliation_attempt_count INTEGER NOT NULL,
	discord_reconciliation_next_attempt_at DATETIME,
	discord_reconciliation_last_error VARCHAR(512),
	published_at DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (activity_id),
	UNIQUE (object_id),
	CONSTRAINT ck_published_object_publication_context CHECK ((publication_context = 'community' AND community_actor_url IS NOT NULL) OR (publication_context IN ('personal_feed','remote_person_feed') AND community_actor_url IS NULL AND discord_guild_id IS NOT NULL AND discord_channel_id IS NOT NULL AND discord_message_id IS NOT NULL))
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
CREATE TABLE remote_person_channel_assignments (
	id INTEGER NOT NULL,
	follow_id INTEGER NOT NULL,
	discord_guild_id INTEGER NOT NULL,
	discord_channel_id INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (follow_id, discord_channel_id),
	FOREIGN KEY(follow_id) REFERENCES remote_person_follows (id) ON DELETE CASCADE
);
CREATE TABLE remote_person_comment_placements (
	id INTEGER NOT NULL,
	root_delivery_id INTEGER NOT NULL,
	inbound_object_id INTEGER,
	published_object_id INTEGER,
	destination_thread_id INTEGER,
	discord_message_id INTEGER,
	state VARCHAR(32) NOT NULL,
	claim_token VARCHAR(64),
	attempt_count INTEGER NOT NULL,
	next_attempt_at DATETIME,
	last_error VARCHAR(512),
	last_applied_revision VARCHAR(64),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (root_delivery_id, inbound_object_id),
	UNIQUE (root_delivery_id, published_object_id),
	UNIQUE (discord_message_id),
	CONSTRAINT ck_remote_person_comment_xor CHECK ((inbound_object_id IS NOT NULL AND published_object_id IS NULL) OR (inbound_object_id IS NULL AND published_object_id IS NOT NULL)),
	CONSTRAINT ck_remote_person_comment_state CHECK (state IN ('pending_parent','pending_thread','pending','claimed','sending','uncertain','delivered','suppressed','failed')),
	CONSTRAINT ck_remote_person_comment_attempts CHECK (attempt_count >= 0 AND attempt_count <= 3),
	FOREIGN KEY(root_delivery_id) REFERENCES remote_person_post_deliveries (id) ON DELETE CASCADE,
	FOREIGN KEY(inbound_object_id) REFERENCES inbound_conversation_objects (id) ON DELETE CASCADE,
	FOREIGN KEY(published_object_id) REFERENCES published_activity_objects (id) ON DELETE CASCADE
);
CREATE TABLE remote_person_follows (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	remote_actor_id VARCHAR(512) NOT NULL,
	remote_inbox_url VARCHAR(512) NOT NULL,
	follow_activity_id VARCHAR(512) NOT NULL,
	generation INTEGER NOT NULL,
	relationship_status VARCHAR(32) NOT NULL,
	follow_dispatch_state VARCHAR(32) NOT NULL,
	undo_dispatch_state VARCHAR(32) NOT NULL,
	follow_claim_token VARCHAR(64),
	undo_claim_token VARCHAR(64),
	last_error VARCHAR(512),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (user_id, remote_actor_id, generation),
	UNIQUE (follow_activity_id),
	CONSTRAINT ck_remote_person_follow_generation CHECK (generation >= 1),
	CONSTRAINT ck_remote_person_relationship_status CHECK (relationship_status IN ('pending','accepted','rejected','cancel_requested','cancelled')),
	CONSTRAINT ck_remote_person_follow_dispatch CHECK (follow_dispatch_state IN ('prepared','sending','sent','uncertain')),
	CONSTRAINT ck_remote_person_undo_dispatch CHECK (undo_dispatch_state IN ('not_requested','prepared','sending','sent','uncertain')),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE TABLE remote_person_post_deliveries (
	id INTEGER NOT NULL,
	post_id INTEGER NOT NULL,
	discord_guild_id INTEGER NOT NULL,
	discord_channel_id INTEGER NOT NULL,
	discord_root_message_id INTEGER,
	discord_thread_id INTEGER,
	webhook_id INTEGER,
	state VARCHAR(32) NOT NULL,
	claim_token VARCHAR(64),
	attempt_count INTEGER NOT NULL,
	next_attempt_at DATETIME,
	last_error VARCHAR(512),
	last_applied_revision VARCHAR(64),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (post_id, discord_channel_id),
	UNIQUE (discord_root_message_id),
	UNIQUE (discord_thread_id),
	CONSTRAINT ck_remote_person_root_delivery_state CHECK (state IN ('pending','claimed','sending','uncertain','delivered','suppressed','failed')),
	CONSTRAINT ck_remote_person_root_thread_id CHECK (discord_thread_id IS NULL OR (discord_root_message_id IS NOT NULL AND discord_thread_id = discord_root_message_id)),
	CONSTRAINT ck_remote_person_root_attempts CHECK (attempt_count >= 0 AND attempt_count <= 3),
	FOREIGN KEY(post_id) REFERENCES remote_person_posts (id) ON DELETE CASCADE
);
CREATE TABLE remote_person_posts (
	id INTEGER NOT NULL,
	object_id VARCHAR(512) NOT NULL,
	create_activity_id VARCHAR(512),
	remote_actor_id VARCHAR(512) NOT NULL,
	remote_handle VARCHAR(255),
	remote_avatar_url VARCHAR(512),
	content_markdown TEXT NOT NULL,
	source_activity_json TEXT NOT NULL,
	revision_fingerprint VARCHAR(64) NOT NULL,
	published_at DATETIME,
	updated_at_remote DATETIME,
	deleted_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (object_id),
	UNIQUE (create_activity_id)
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
CREATE TABLE user_actor_content_mutations (
	id INTEGER NOT NULL,
	published_object_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	kind VARCHAR(32) NOT NULL,
	expected_revision INTEGER NOT NULL,
	next_revision INTEGER NOT NULL,
	operation_id VARCHAR(64) NOT NULL,
	activity_id VARCHAR(512) NOT NULL,
	request_json VARCHAR NOT NULL,
	request_fingerprint VARCHAR(64) NOT NULL,
	state VARCHAR(32) NOT NULL,
	canonical_activity_json VARCHAR,
	claim_token VARCHAR(64),
	side_effect_classification VARCHAR(32) NOT NULL,
	lease_expires_at DATETIME,
	claimed_at DATETIME,
	attempt_count INTEGER NOT NULL,
	next_attempt_at DATETIME,
	last_error VARCHAR(512),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (operation_id),
	UNIQUE (activity_id),
	CONSTRAINT ck_user_actor_content_mutation_kind CHECK (kind IN ('content_update', 'content_delete')),
	CONSTRAINT ck_user_actor_content_mutation_state CHECK (state IN ('reserved', 'dispatching', 'ambiguous', 'canonical_ready', 'committed', 'aborted_pre_send')),
	FOREIGN KEY(published_object_id) REFERENCES published_activity_objects (id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE TABLE user_actor_follow_activity_ids (
	id INTEGER NOT NULL,
	follow_activity_id VARCHAR(512) NOT NULL,
	user_id INTEGER NOT NULL,
	remote_actor_id VARCHAR(512) NOT NULL,
	first_seen_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (follow_activity_id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE TABLE user_actor_followers (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
	user_id INTEGER NOT NULL,
	remote_actor_id VARCHAR(512) NOT NULL,
	remote_inbox_url VARCHAR(512) NOT NULL,
	remote_shared_inbox_url VARCHAR(512),
	follow_activity_id VARCHAR(512) NOT NULL,
	status VARCHAR(32) NOT NULL,
	owner_notification_state VARCHAR(32) NOT NULL,
	owner_notification_attempted_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	UNIQUE (user_id, remote_actor_id),
	CONSTRAINT ck_user_actor_follower_status CHECK (status = 'accepted'),
	CONSTRAINT ck_user_actor_follower_notification_state CHECK (owner_notification_state IN ('pending', 'attempted')),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE TABLE user_actor_outbound_deliveries (
	id INTEGER NOT NULL,
	source_id INTEGER NOT NULL,
	target_endpoint_url VARCHAR(512) NOT NULL,
	status VARCHAR(32) NOT NULL,
	claim_token VARCHAR(64),
	claimed_at DATETIME,
	attempt_count INTEGER NOT NULL,
	last_attempted_at DATETIME,
	delivered_at DATETIME,
	last_error VARCHAR(512),
	next_attempt_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_id, target_endpoint_url),
	CONSTRAINT ck_user_actor_delivery_status CHECK (status IN ('pending', 'sending', 'delivered', 'failed', 'ineligible', 'superseded', 'unknown')),
	CONSTRAINT ck_user_actor_delivery_attempts CHECK (attempt_count >= 0),
	CONSTRAINT ck_user_actor_delivery_endpoint_nonempty CHECK (length(target_endpoint_url) > 0),
	FOREIGN KEY(source_id) REFERENCES user_actor_outbound_sources (id) ON DELETE CASCADE
);
CREATE TABLE user_actor_outbound_delivery_followers (
	id INTEGER NOT NULL,
	delivery_id INTEGER NOT NULL,
	relationship_id_snapshot INTEGER NOT NULL,
	remote_actor_id VARCHAR(512) NOT NULL,
	snapshotted_endpoint_url VARCHAR(512) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (delivery_id, relationship_id_snapshot),
	FOREIGN KEY(delivery_id) REFERENCES user_actor_outbound_deliveries (id) ON DELETE CASCADE
);
CREATE TABLE user_actor_outbound_source_media (
	id INTEGER NOT NULL,
	source_id INTEGER NOT NULL,
	media_asset_id INTEGER NOT NULL,
	slot_kind VARCHAR(16) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_id, slot_kind),
	CONSTRAINT ck_user_actor_source_media_slot CHECK (slot_kind IN ('avatar', 'header')),
	FOREIGN KEY(source_id) REFERENCES user_actor_outbound_sources (id) ON DELETE CASCADE,
	FOREIGN KEY(media_asset_id) REFERENCES profile_media_assets (id) ON DELETE RESTRICT
);
CREATE TABLE user_actor_outbound_sources (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	source_kind VARCHAR(32) NOT NULL,
	ordering_key VARCHAR(512) NOT NULL,
	logical_version INTEGER NOT NULL,
	activity_id VARCHAR(512) NOT NULL,
	published_object_id INTEGER,
	snapshot_json VARCHAR NOT NULL,
	activity_json VARCHAR,
	state VARCHAR(32) NOT NULL,
	render_attempt_count INTEGER NOT NULL,
	last_render_attempted_at DATETIME,
	last_error VARCHAR(512),
	next_attempt_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (activity_id),
	UNIQUE (ordering_key, logical_version),
	CONSTRAINT ck_user_actor_source_version CHECK (logical_version >= 1),
	CONSTRAINT ck_user_actor_source_snapshot_nonempty CHECK (length(snapshot_json) > 0),
	CONSTRAINT ck_user_actor_source_kind CHECK (source_kind IN ('profile_update', 'content_create', 'content_update', 'content_delete')),
	CONSTRAINT ck_user_actor_source_state CHECK (state IN ('render_pending', 'ready', 'render_failed')),
	CONSTRAINT ck_user_actor_source_render_attempts CHECK (render_attempt_count >= 0),
	CONSTRAINT ck_user_actor_source_activity_state CHECK ((state = 'ready' AND activity_json IS NOT NULL) OR (state != 'ready' AND activity_json IS NULL)),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(published_object_id) REFERENCES published_activity_objects (id) ON DELETE CASCADE
);
CREATE TABLE user_profile_fields (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	position INTEGER NOT NULL,
	name VARCHAR(255) NOT NULL,
	value VARCHAR(255) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (user_id, position),
	CONSTRAINT ck_user_profile_field_position CHECK (position >= 0 AND position <= 3),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
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
	display_name VARCHAR(30),
	summary VARCHAR(500),
	avatar_media_id INTEGER,
	header_media_id INTEGER,
	profile_version INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (discord_user_id),
	UNIQUE (activitypub_username),
	UNIQUE (actor_url),
	FOREIGN KEY(avatar_media_id) REFERENCES profile_media_assets (id) ON DELETE RESTRICT,
	FOREIGN KEY(header_media_id) REFERENCES profile_media_assets (id) ON DELETE RESTRICT
);
CREATE INDEX ix_bridge_policy_entries_normalized_subject ON bridge_policy_entries (normalized_subject);
CREATE INDEX ix_bridge_policy_entries_policy_type ON bridge_policy_entries (policy_type);
CREATE INDEX ix_bridge_policy_entries_status ON bridge_policy_entries (status);
CREATE INDEX ix_community_actor_bans_target_discord_user_id ON community_actor_bans (target_discord_user_id);
CREATE INDEX ix_inbound_conversation_root_parent ON inbound_conversation_objects (conversation_root_object_id, parent_object_id);
CREATE INDEX ix_inbound_discord_due ON inbound_discord_deliveries (state, next_attempt_at, id);
CREATE INDEX ix_management_audit_events_action ON management_audit_events (action);
CREATE INDEX ix_management_audit_events_actor_discord_user_id ON management_audit_events (actor_discord_user_id);
CREATE INDEX ix_management_audit_events_created_at ON management_audit_events (created_at);
CREATE INDEX ix_management_audit_events_local_community_id ON management_audit_events (local_community_id);
CREATE INDEX ix_management_audit_events_reason_code ON management_audit_events (reason_code);
CREATE INDEX ix_management_audit_events_result ON management_audit_events (result);
CREATE INDEX ix_management_audit_events_target_id ON management_audit_events (target_id);
CREATE INDEX ix_management_audit_events_target_type ON management_audit_events (target_type);
CREATE INDEX ix_published_activity_objects_conversation_root_object_id ON published_activity_objects (conversation_root_object_id);
CREATE INDEX ix_published_objects_user_outbox ON published_activity_objects (actor_username, actor_url, deleted_at, published_at, id);
CREATE INDEX ix_remote_person_assignments_target ON remote_person_channel_assignments (discord_guild_id, discord_channel_id);
CREATE INDEX ix_remote_person_comment_due ON remote_person_comment_placements (state, next_attempt_at, id);
CREATE INDEX ix_remote_person_follow_actor ON remote_person_follows (remote_actor_id, relationship_status);
CREATE INDEX ix_remote_person_post_actor ON remote_person_posts (remote_actor_id, published_at, id);
CREATE INDEX ix_remote_person_root_due ON remote_person_post_deliveries (state, next_attempt_at, id);
CREATE INDEX ix_user_actor_content_mutations_recovery ON user_actor_content_mutations (state, next_attempt_at, claimed_at, id);
CREATE INDEX ix_user_actor_deliveries_endpoint ON user_actor_outbound_deliveries (target_endpoint_url, status, source_id);
CREATE INDEX ix_user_actor_deliveries_retry ON user_actor_outbound_deliveries (status, next_attempt_at, claimed_at);
CREATE INDEX ix_user_actor_delivery_followers_relationship ON user_actor_outbound_delivery_followers (relationship_id_snapshot, delivery_id);
CREATE INDEX ix_user_actor_follow_activity_owner ON user_actor_follow_activity_ids (user_id, remote_actor_id);
CREATE INDEX ix_user_actor_followers_collection ON user_actor_followers (user_id, status, id);
CREATE INDEX ix_user_actor_source_media_asset ON user_actor_outbound_source_media (media_asset_id, source_id);
CREATE INDEX ix_user_actor_sources_retry ON user_actor_outbound_sources (state, next_attempt_at, user_id, ordering_key, logical_version);
CREATE UNIQUE INDEX uq_ap_receipt_global ON activitypub_event_receipts (delivery_id) WHERE verified_destination_group_actor_id IS NULL;
CREATE UNIQUE INDEX uq_ap_receipt_local_group ON activitypub_event_receipts (delivery_id, event_type, verified_destination_group_actor_id) WHERE verified_destination_group_actor_id IS NOT NULL;
CREATE UNIQUE INDEX uq_bridge_follow_activity ON bridge_actor_follows (follow_activity_id) WHERE follow_activity_id IS NOT NULL;
CREATE UNIQUE INDEX uq_discord_target_message ON community_discord_target_claims (message_group_id, discord_channel_id) WHERE message_group_id IS NOT NULL;
CREATE UNIQUE INDEX uq_discord_target_thread ON community_discord_target_claims (thread_group_id, discord_channel_id) WHERE thread_group_id IS NOT NULL;
CREATE UNIQUE INDEX uq_local_relay_create ON local_community_relay_source_activities (local_community_id, source_object_ap_id) WHERE operation = 'create';
CREATE UNIQUE INDEX uq_remote_message_group_object ON community_message_groups (community_actor_id, ap_object_id) WHERE ap_object_id IS NOT NULL AND source_channel_id IS NULL AND source_message_id IS NULL;
CREATE UNIQUE INDEX uq_remote_person_live_follow ON remote_person_follows (user_id, remote_actor_id) WHERE relationship_status IN ('pending','accepted','cancel_requested');
CREATE UNIQUE INDEX uq_remote_thread_group_object ON community_thread_groups (community_actor_id, ap_object_id) WHERE ap_object_id IS NOT NULL AND source_channel_id IS NULL AND source_thread_id IS NULL;
CREATE UNIQUE INDEX uq_source_discord_reply_message ON community_message_groups (source_message_id) WHERE source_message_id IS NOT NULL;
CREATE TRIGGER users_avatar_media_kind_insert
        BEFORE INSERT ON users WHEN NEW.avatar_media_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.avatar_media_id) != 'avatar'
            THEN RAISE(ABORT, 'avatar_media_id must reference avatar media') END;
        END;
CREATE TRIGGER users_avatar_media_kind_update
        BEFORE UPDATE OF avatar_media_id ON users WHEN NEW.avatar_media_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.avatar_media_id) != 'avatar'
            THEN RAISE(ABORT, 'avatar_media_id must reference avatar media') END;
        END;
CREATE TRIGGER users_header_media_kind_insert
        BEFORE INSERT ON users WHEN NEW.header_media_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.header_media_id) != 'header'
            THEN RAISE(ABORT, 'header_media_id must reference header media') END;
        END;
CREATE TRIGGER users_header_media_kind_update
        BEFORE UPDATE OF header_media_id ON users WHEN NEW.header_media_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.header_media_id) != 'header'
            THEN RAISE(ABORT, 'header_media_id must reference header media') END;
        END;
CREATE UNIQUE INDEX uq_remote_person_assignment_follow_guild ON remote_person_channel_assignments (follow_id, discord_guild_id);
