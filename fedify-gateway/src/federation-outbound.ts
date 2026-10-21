/**
 * Compatibility exports for gateway outbound use cases.
 *
 * initial persistence implementation moves implementation into focused modules under `src/outbound/` while
 * preserving existing imports from `federation-outbound.ts`. This file must stay
 * logic-free so route extraction and caller import cleanup remain isolated from
 * the outbound decomposition.
 */

export { buildGatewayUrl } from "./outbound/gateway-url.js";
export { renderPublicActivityJson } from "./outbound/activity-json.js";
export { followCommunity, unfollowCommunity, buildLemmyCompatibleUnfollowActivity } from "./outbound/community-follow.js";
export { acceptUserActorFollow, acceptLocalCommunityFollow } from "./outbound/follow-accept.js";
export { publishContent, buildPublishCreateActivity } from "./outbound/content-publish.js";
export { publishLocalCommunityContent, buildLocalCommunityAnnounceActivity } from "./outbound/local-community-fanout.js";
export { sendLocalCommunityRelay } from "./outbound/local-community-relay.js";
export { renderPersonalFeedCreate } from "./outbound/personal-feed-renderer.js";
export { updateContent, deleteContent, verifyMutationIdentity } from "./outbound/content-mutations.js";
export { sendUserActorDeliveries } from "./outbound/user-actor-delivery.js";
export type { FollowCommunityResult } from "./outbound/community-follow.js";
