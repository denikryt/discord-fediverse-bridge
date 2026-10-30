/** Compile the dormant write factories against the pinned official SDK Client.
 *
 * This module is checked by the ordinary tsc include. It never starts a
 * session or registers writes in production main.ts; live acceptance still
 * controls production capabilities and RPC dispatch.
 */
import {type Client, Routes} from '@fluxerjs/core';
import {createSdkForumCreateHandler} from './forum_create.ts';
import {createSdkMessageSendHandler} from './message_send.ts';
import {createSdkMessageMutationHandler} from './message_mutations.ts';

/** Bind all isolated executors without inventing a narrower SDK client shape. */
export function verifySdkWriteContracts(client: Client): void {
  createSdkForumCreateHandler(client, Routes.channel, Routes.channelThreads);
  createSdkMessageSendHandler(client);
  createSdkMessageMutationHandler(client);
}
