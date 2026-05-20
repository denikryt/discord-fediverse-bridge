export interface BridgeObject {
  ap_id: string;
  author_name: string;
  body_markdown: string | null;
  kind: "post" | "comment";
  lemmy_id: number;
  parent_ap_id: string | null;
  post_ap_id: string | null;
  post_lemmy_id: number | null;
  published_at: string;
  title: string | null;
  url: string;
}

export interface BridgeContentEvent {
  actor_id: string;
  community_actor_id: string;
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
}

export interface LocalFollowRequestedEvent {
  actor_id: string;
  community_actor_id: string;
  delivery_id: string;
  event_type: "local.follow_requested";
  object: LocalFollowRequestedObject;
  occurred_at: string;
}

export type InternalBridgeEvent =
  | BridgeContentEvent
  | FollowAcceptedEvent
  | LocalFollowRequestedEvent;

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
}

export interface PublishLocalCommunityContentRequest {
  actorUsername: string;
  communityActorUrl: string;
  kind: "post" | "comment";
  title: string | null;
  bodyMarkdown: string;
  inReplyToObjectId: string | null;
}

export interface PublishLocalCommunityContentResult {
  activityId: string;
  objectId: string;
  communityActorUrl: string;
  deliveredFollowerCount: number;
  failedFollowerCount: number;
}

export interface UpdateContentRequest {
  // actorUsername must match the original attributedTo — Lemmy enforces this
  // ownership check and rejects Updates from any other actor.
  actorUsername: string;
  communityActorUrl: string;
  apObjectId: string;
  kind: "post" | "comment";
  bodyMarkdown: string;
  // title is required for posts only; ignored for comments.
  title?: string | null;
  // inReplyToObjectId is required for comments to identify the parent post.
  // Lemmy will not process comment updates without this field.
  inReplyToObjectId?: string | null;
}

export interface DeleteContentRequest {
  // actorUsername must match the original attributedTo — same ownership rule as Update.
  actorUsername: string;
  communityActorUrl: string;
  apObjectId: string;
}

export interface AcceptLocalCommunityFollowRequest {
  communitySlug: string;
  communityActorUrl: string;
  remoteActorId: string;
  remoteInboxUrl: string;
  followActivityId: string;
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
