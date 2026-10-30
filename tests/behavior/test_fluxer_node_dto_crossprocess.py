"""Raw Fluxer Gateway observations cross the Node normalization/Python DTO contract."""

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

    def test_six_documented_gateway_events_decode_as_existing_python_types(self) -> None:
        """A bot observes six Fluxer updates; Python accepts exact typed DTOs."""
        key = 'fx-test'
        thread = {
            'id': 'thread-1', 'guild_id': 'guild-1', 'parent_id': 'forum-1',
            'type': 11, 'name': 'Topic',
        }
        message = {
            'id': 'msg-1', 'channel_id': 'thread-1', 'guild_id': 'guild-1',
            'author': {'id': 'user-1', 'username': 'Ada', 'bot': False},
            'content': 'Hello', 'type': 0,
            'timestamp': '2026-10-27T10:00:00.000Z',
        }
        observations = [
            ('THREAD_CREATE', {**thread, 'newly_created': True}),
            ('THREAD_UPDATE', {**thread, 'name': 'Renamed'}),
            ('THREAD_DELETE', {key: thread[key] for key in ('id', 'guild_id', 'parent_id', 'type')}),
            ('MESSAGE_CREATE', message),
            ('MESSAGE_UPDATE', {'id': 'msg-1', 'channel_id': 'thread-1'}),
            ('MESSAGE_DELETE', {'id': 'msg-1', 'channel_id': 'thread-1'}),
        ]
        # Keep all observed IDs as strings across the Node/Python JSON boundary.
        script = '''
          import {normalizeGatewayEvent} from './fluxer-connector/src/normalization.ts';
          let bytes = '';
          for await (const part of process.stdin) bytes += part;
          const observations = JSON.parse(bytes);
          console.log(JSON.stringify(observations.map(([type, body]) =>
            normalizeGatewayEvent('fx-test', type, body,
              {receivedAt:'2026-10-27T11:00:00.000Z'}))));
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
