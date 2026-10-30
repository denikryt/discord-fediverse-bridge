"""Typed SDK observations cross the Node normalization/Python DTO contract."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from src.internal_fluxer_api import decode_fluxer_event_wire
from src.platform_types import (
    ThreadCreated, ThreadUpdated, ThreadDeleted,
    MessageCreated, MessageUpdated, MessageDeleted,
)


class NodeFluxerWireContractScenarios(unittest.TestCase):
    """Check actual subprocess serialization, not independently duplicated fixtures."""

    def test_six_sdk_events_decode_as_existing_python_types(self) -> None:
        """A bot observes six Fluxer updates; Python accepts exact typed DTOs."""
        key = 'fx-test'
        thread = {
            'id': 'thread-1', 'guildId': 'guild-1', 'parentId': 'forum-1',
            'type': 11, 'name': 'Topic',
        }
        message = {
            'id': 'msg-1', 'channelId': 'thread-1', 'guildId': 'guild-1',
            'author': {'id': 'user-1', 'username': 'Ada', 'bot': False},
            'content': 'Hello', 'type': 0,
            'createdAt': '2026-10-27T10:00:00.000Z',
        }
        observations = [
            ('threadCreate', [thread]),
            ('threadUpdate', [thread, {**thread, 'name': 'Renamed'}]),
            ('threadDelete', [thread]),
            ('messageCreate', [message]),
            ('messageUpdate', [None, {'id': 'msg-1', 'channelId': 'thread-1'}]),
            ('messageDelete', [{'id': 'msg-1', 'channelId': 'thread-1'}]),
        ]
        # Keep all observed IDs as strings across the Node/Python JSON boundary.
        script = '''
          import {normalizeSdkEvent} from './fluxer-connector/src/normalization.ts';
          let bytes = '';
          for await (const part of process.stdin) bytes += part;
          const observations = JSON.parse(bytes);
          console.log(JSON.stringify(observations.map(([type, args]) => {
            // JSON cannot represent Date or SDK Collection; hydrate the observed
            // values as the corresponding public SDK model fields.
            if (type === 'messageCreate') {
              args[0].createdAt = new Date(args[0].createdAt);
              args[0].attachments = new Map();
            }
            return normalizeSdkEvent('fx-test', type, args,
              {receivedAt:'2026-10-27T11:00:00.000Z'});
          })));
        '''
        root = Path(__file__).resolve().parents[2]
        completed = subprocess.run(
            ['node', '--experimental-strip-types', '--input-type=module', '-e', script],
            cwd=root, input=json.dumps(observations), text=True, capture_output=True,
            timeout=10, check=True,
        )
        wire = json.loads(completed.stdout)
        decoded = tuple(decode_fluxer_event_wire(item) for item in wire)
        self.assertEqual(tuple(type(item) for item in decoded), (
            ThreadCreated, ThreadUpdated, ThreadDeleted,
            MessageCreated, MessageUpdated, MessageDeleted,
        ))
        self.assertIsNone(decoded[0].starter)
        self.assertEqual(decoded[0].thread.channel.kind, 'unknown')
        self.assertEqual(decoded[3].created_at.isoformat(), '2026-10-27T10:00:00+00:00')
        self.assertIsNone(decoded[4].content)
        self.assertIsNone(decoded[5].parent_channel)
        self.assertEqual(decoded[5].message.external_container_id, 'thread-1')
        self.assertFalse(any('sequence' in item or 'epoch' in item for item in wire))


if __name__ == '__main__':
    unittest.main()
