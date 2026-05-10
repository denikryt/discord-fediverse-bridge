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
  event_type: "post.created" | "comment.created";
  object: BridgeObject;
  occurred_at: string;
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

export type InternalBridgeEvent = BridgeContentEvent | FollowAcceptedEvent;

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
}

export interface DeleteContentRequest {
  // actorUsername must match the original attributedTo — same ownership rule as Update.
  actorUsername: string;
  communityActorUrl: string;
  apObjectId: string;
}
