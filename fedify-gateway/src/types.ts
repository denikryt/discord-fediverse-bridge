export interface BridgeTarget {
  kind: "community" | "personal_feed";
  root_object_id?: string | null;
}

export interface BridgeObject {
  ap_id: string;
  author_name: string;
  body_markdown: string | null;
  kind: "post" | "comment";
  lemmy_id: number | null;
  parent_ap_id: string | null;
  post_ap_id: string | null;
  post_lemmy_id: number | null;
  published_at: string;
  /** Optional verified source Update clock, distinct from original publication. */
  updated_at?: string | null;
  title: string | null;
  url: string;
  attributed_to_actor_id?: string | null;
}

export interface BridgeContentEvent {
  actor_id: string;
  community_actor_id?: string | null;
  target?: BridgeTarget | null;
  delivery_id: string;
  event_type:
    | "post.created"
    | "comment.created"
    | "post.updated"
    | "comment.updated"
    | "post.deleted"
    | "comment.deleted";
  object: BridgeObject;
  occurred_at: string;
  source_activity_json?: Record<string, unknown> | null;
  source_activity_id?: string | null;
  source_announce_id?: string | null;
}

// BridgeEvent stays as the content-event alias so the existing normalization
// pipeline keeps its current naming while follow lifecycle uses a separate type.
export type BridgeEvent = BridgeContentEvent;

export interface FollowLifecycleObject {
  follow_activity_id: string;
}

export interface FollowAcceptedEvent {
  actor_id: string;
  community_actor_id: string;
  delivery_id: string;
  event_type: "follow.accepted";
  object: FollowLifecycleObject;
  occurred_at: string;
}

export interface LocalFollowRequestedObject {
  follow_activity_id: string;
  remote_inbox_url: string;
  remote_shared_inbox_url: string | null;
}

export interface LocalFollowRequestedEvent {
  actor_id: string;
  target_actor_kind: "community" | "user";
  target_actor_id: string;
  delivery_id: string;
  event_type: "local.follow_requested";
  object: LocalFollowRequestedObject;
  occurred_at: string;
}

export interface LocalUnfollowRequestedObject {
  follow_activity_id: string | null;
}

export interface LocalUnfollowRequestedEvent {
  actor_id: string;
  target_actor_kind: "community" | "user";
  target_actor_id: string;
  delivery_id: string;
  event_type: "local.unfollow_requested";
  object: LocalUnfollowRequestedObject;
  occurred_at: string;
}

export type InternalBridgeEvent =
  | BridgeContentEvent
  | FollowAcceptedEvent
  | LocalFollowRequestedEvent
  | LocalUnfollowRequestedEvent;

export interface AcceptUserActorFollowRequest {
  actorUsername: string;
  actorUrl: string;
  remoteActorId: string;
  remoteInboxUrl: string;
  followActivityId: string;
}

export interface RenderPersonalFeedRequest {
  actorUsername: string;
  operationId: string;
  activityId: string;
  objectId: string;
  bodyMarkdown: string;
  publishedAt: string;
  kind?: "post" | "comment";
  inReplyToObjectId?: string | null;
  conversationRootObjectId?: string | null;
}

export interface RenderPersonalFeedResult {
  activityId: string;
  objectId: string;
  publishedAt: string;
  activityJson: Record<string, unknown>;
}

/** Pure community Create build request; immutable source coordinates come from Python. */
export interface RenderCommunityRequest {
  actorUsername: string;
  communityActorUrl: string;
  kind: "post" | "comment";
  title: string | null;
  bodyMarkdown: string;
  inReplyToObjectId: string | null;
  operationId: string;
  activityId: string;
  objectId: string;
  publishedAt: string;
}

export interface RenderCommunityResult {
  activityId: string;
  objectId: string;
  communityActorUrl: string;
  publishedAt: string;
  activityJson: Record<string, unknown>;
}

export interface PublishContentRequest {
  actorUsername: string;
  communityActorUrl: string;
  kind: "post" | "comment";
  title: string | null;
  bodyMarkdown: string;
  inReplyToObjectId: string | null;
}

export interface PublishContentResult {
  activityId: string;
  objectId: string;
  communityActorUrl: string;
  activityJson: Record<string, unknown>;
}

export interface PublishLocalCommunityContentRequest {
  actorUsername: string;
  communityActorUrl: string;
  kind: "post" | "comment";
  title: string | null;
  bodyMarkdown: string;
  inReplyToObjectId: string | null;
  targetInboxUrls?: string[] | null;
}

export interface PublishLocalCommunityContentResult {
  activityId: string;
  objectId: string;
  communityActorUrl: string;
  deliveredFollowerCount: number;
  failedFollowerCount: number;
  activityJson: Record<string, unknown>;
}

export interface CanonicalMutationResult {
  activityId: string;
  objectId: string;
  activityJson: Record<string, unknown>;
}

export interface UpdateContentRequest {
  // actorUsername must match the original attributedTo — Lemmy enforces this
  // ownership check and rejects Updates from any other actor.
  actorUsername: string;
  publicationContext?: "community" | "personal_feed";
  communityActorUrl: string | null;
  conversationRootObjectId?: string | null;
  apObjectId: string;
  kind: "post" | "comment";
  bodyMarkdown: string;
  // title is required for posts only; ignored for comments.
  title?: string | null;
  // inReplyToObjectId is required for comments to identify the parent post.
  // Lemmy will not process comment updates without this field.
  inReplyToObjectId?: string | null;
  operationId?: string | null;
  activityId?: string | null;
  mutationTimestamp?: string | null;
  expectedRevision?: number | null;
  nextRevision?: number | null;
}

export interface DeleteContentRequest {
  // actorUsername must match the original attributedTo — same ownership rule as Update.
  actorUsername: string;
  publicationContext?: "community" | "personal_feed";
  communityActorUrl: string | null;
  conversationRootObjectId?: string | null;
  apObjectId: string;
  operationId?: string | null;
  activityId?: string | null;
  mutationTimestamp?: string | null;
  expectedRevision?: number | null;
  nextRevision?: number | null;
}

export interface AcceptLocalCommunityFollowRequest {
  communitySlug: string;
  communityActorUrl: string;
  remoteActorId: string;
  remoteInboxUrl: string;
  followActivityId: string;
}

export interface LocalCommunityDiscoveryRecord {
  id: number;
  slug: string;
  name: string;
  title: string;
  description: string | null;
  actor_id: string;
  alternate_actor_id: string;
  handle: string;
}


export interface SendLocalCommunityRelayDelivery {
  deliveryId: number;
  targetRemoteActorId: string;
  targetInboxUrl: string;
  activityJson: Record<string, unknown>;
}

export interface SendLocalCommunityRelayRequest {
  signingActorUrl: string;
  deliveries: SendLocalCommunityRelayDelivery[];
}

export interface SendLocalCommunityRelayOutcome {
  deliveryId: number;
  targetRemoteActorId: string;
  ok: boolean;
  activityId: string | null;
  error: string | null;
}

export interface SendLocalCommunityRelayResult {
  outcomes: SendLocalCommunityRelayOutcome[];
}

export interface UserActorProfileSnapshot {
  schemaVersion: number;
  actorId: string;
  username: string;
  displayName: string;
  summary: string | null;
  inboxId: string;
  outboxId: string;
  followersId: string;
  publicKeyId: string;
  publicKeyPem: string;
  fields: { name: string; value: string }[];
  avatar: { url: string; contentType: string; width: number; height: number } | null;
  header: { url: string; contentType: string; width: number; height: number } | null;
}

export interface RenderUserActorProfileUpdateRequest {
  activityId: string;
  profileSnapshot: UserActorProfileSnapshot;
}

export interface SendUserActorDelivery {
  deliveryId: number;
  targetEndpointUrl: string;
  activityJson: Record<string, unknown>;
}

export interface SendUserActorDeliveriesRequest {
  signingActorUrl: string;
  actorUsername: string;
  deliveries: SendUserActorDelivery[];
}
