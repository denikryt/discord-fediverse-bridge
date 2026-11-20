/** Scope full REST/SDK messages using an independently verified guild container.
 * Fluxer REST messages omit guild_id; SDK 3.3.0 exposes that omission as null.
 * This boundary never supplies guild identity to an unverified channel or DTO.
 */

/** Require the exact container and reject every explicit conflicting guild.
 * Existing-message callers must fetch the guild container with force:true;
 * forum receipt callers validate the returned thread against its fresh parent.
 * A missing SDK property (undefined) is an incomplete provider object, not the
 * null that the pinned SDK constructs from a full REST message.
 */
export function matchesMessageScope(
  message: Readonly<{channelId: unknown; guildId: unknown}>,
  verifiedContainerId: string, verifiedGuildId: string,
): boolean {
  return message.channelId === verifiedContainerId &&
    (message.guildId === null || message.guildId === verifiedGuildId);
}
