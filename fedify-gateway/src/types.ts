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

export interface BridgeEvent {
  actor_id: string;
  community_actor_id: string;
  delivery_id: string;
  event_type: "post.created" | "comment.created";
  object: BridgeObject;
  occurred_at: string;
}
