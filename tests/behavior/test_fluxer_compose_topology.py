"""Security regression checks for the optional two-container Fluxer deployment."""

from __future__ import annotations

from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


class FluxerComposeTopologyContracts(unittest.TestCase):
    """Reject network namespace drift and Python bot-token mounts at configuration."""

    @classmethod
    def setUpClass(cls) -> None:
        """Load exactly the production Compose declaration, without starting Docker."""
        cls.compose = yaml.safe_load((ROOT / 'compose.yaml').read_text())

    def test_one_opt_in_sidecar_shares_bridge_loopback_only(self) -> None:
        """A second SDK owner has no independent network/host RPC exposure."""
        services = self.compose['services']
        node = services['fluxer-sidecar']
        self.assertEqual(node['network_mode'], 'service:bridge')
        self.assertEqual(node['profiles'], ['fluxer'])
        self.assertNotIn('ports', node)
        self.assertNotIn('networks', node)
        self.assertNotIn('network_mode', services['bridge'])
        self.assertNotIn('fluxer-sidecar', services['bridge'].get('depends_on', {}))
        self.assertEqual(node['depends_on']['bridge']['condition'], 'service_healthy')
        self.assertEqual(node['environment']['FLUXER_RPC_PORT'], '8769')
        self.assertEqual(node['environment']['FLUXER_PYTHON_INGRESS_URL'],
                         'http://127.0.0.1:8080/internal/fluxer/events')
        self.assertIn('healthcheck', node)

    def test_selected_platform_is_explicit_and_wrong_profile_mode_exits(self) -> None:
        """Opt-in sidecar refuses a Discord-mode Python without mounting a bot there."""
        services = self.compose['services']
        expected = '${BRIDGE_PLATFORM-discord}'
        self.assertEqual(services['bridge']['environment']['BRIDGE_PLATFORM'], expected)
        node = services['fluxer-sidecar']
        self.assertEqual(node['environment']['BRIDGE_PLATFORM'], expected)
        script = ' '.join(node['command'])
        self.assertIn('$$BRIDGE_PLATFORM', script)
        self.assertIn('!= "fluxer"', script)
        self.assertIn('exit 1', script)
        self.assertIn('exec node --experimental-strip-types src/main.ts', script)
        # An explicit deployment-mode mismatch is a hard startup error, not
        # automatic in-process platform switching or a second Discord client.
        self.assertNotIn('profiles', services['bridge'])

    def test_healthcheck_requires_expected_instance_origin_and_sdk_identity(self) -> None:
        """Listening or a cached bot ID alone cannot make the sidecar healthy."""
        node = self.compose['services']['fluxer-sidecar']
        probe = ' '.join(str(x) for x in node['healthcheck']['test'])
        self.assertIn('instance_origin', probe)
        self.assertIn('FLUXER_ORIGIN', probe)
        self.assertIn('bot_id', probe)
        self.assertIn('gateway_gap', probe)

    def test_healthcheck_canonicalizes_origin_but_rejects_foreign_ready(self) -> None:
        """Evaluate the production JS probe against local READY/negative RPC responses."""
        import json
        import os
        import shutil
        import subprocess

        if shutil.which('node') is None:
            self.skipTest('Node runtime unavailable')
        node = self.compose['services']['fluxer-sidecar']
        command = node['healthcheck']['test']
        script = command[command.index('-e') + 1]
        baseline = {
            'ok': True, 'result': {'state': 'ready', 'connector_key': 'fx-one',
                                   'instance_origin': 'https://fluxer.test',
                                   'bot_id': 'bot-1', 'gateway_gap': False},
        }

        def probe(origin: str, body: dict) -> int:
            """Run the real shell-free healthcheck with only SDK HTTP mocked."""
            fake = ('globalThis.fetch=async()=>({ok:true, json:async()=>('
                    + json.dumps(body) + ')});\n')
            env = {**os.environ, 'FLUXER_ORIGIN': origin,
                   'FLUXER_CONNECTOR_KEY': 'fx-one', 'FLUXER_INTERNAL_BEARER': 'secret'}
            return subprocess.run(['node', '-e', fake + script], env=env,
                                  capture_output=True, timeout=8).returncode

        for origin in ('https://fluxer.test', 'https://FLUXER.test',
                       'https://fluxer.test:443'):
            self.assertEqual(probe(origin, baseline), 0, origin)
        for key, value in [('instance_origin', 'https://foreign.test'),
                           ('connector_key', 'foreign'), ('bot_id', ''),
                           ('state', 'degraded'), ('gateway_gap', True)]:
            body = {'ok': True, 'result': {**baseline['result'], key: value}}
            self.assertNotEqual(probe('https://fluxer.test', body), 0, key)
        self.assertNotEqual(probe('invalid-origin', baseline), 0)

    def test_bot_secret_mount_exists_only_in_node(self) -> None:
        """The Python env/mount topology never acquires the real Node token."""
        services = self.compose['services']
        node = services['fluxer-sidecar']
        mounts = node.get('secrets', [])
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0]['source'], 'fluxer-bot-token')
        self.assertEqual(node['environment']['FLUXER_BOT_TOKEN_FILE'],
                         '/run/secrets/fluxer-bot-token')
        self.assertNotIn('FLUXER_BOT_TOKEN', services['bridge']['environment'])
        self.assertNotIn('secrets', services['bridge'])
        self.assertNotIn('fluxer-sidecar', services['backup'].get('depends_on', {}))
        self.assertNotIn('FLUXER_BOT_TOKEN', node['environment'])

    def test_node_secret_file_is_gitignored_without_ignoring_source(self) -> None:
        """A local bot token never becomes an accidental Git working-tree change."""
        import subprocess
        secret = subprocess.run(['git', 'check-ignore', '--quiet', '--no-index',
                                 'secrets/fluxer-bot-token'], cwd=ROOT)
        source = subprocess.run(['git', 'check-ignore', '--quiet', '--no-index',
                                 'fluxer-connector/package.json'], cwd=ROOT)
        self.assertEqual(secret.returncode, 0)
        self.assertEqual(source.returncode, 1)

    def test_release_requires_pinned_lock_and_matching_image_version(self) -> None:
        """Never publish an optional image from floating npm dependencies."""
        node = ROOT / 'fluxer-connector'
        dockerfile = (node / 'Dockerfile').read_text()
        workflow = (ROOT / '.github/workflows/release.yml').read_text()
        self.assertIn('COPY fluxer-connector/package.json fluxer-connector/package-lock.json',
                      dockerfile)
        self.assertIn('npm ci --ignore-scripts', dockerfile)
        self.assertNotIn('RUN npm install', dockerfile)
        self.assertIn('node fluxer-connector/scripts/check-lock.mjs', workflow)
        self.assertNotIn('fluxer_lock', workflow)
        jobs = yaml.safe_load(workflow)['jobs']
        self.assertNotIn('if', jobs['publish-fluxer-image'])
        self.assertNotIn("== 'skipped'", jobs['create-release']['if'])
        self.assertIn("node-version: '22'", workflow)
        self.assertNotIn('hashFiles(', workflow)
        self.assertIn('file: fluxer-connector/Dockerfile', workflow)
        import json
        package = json.loads((node / 'package.json').read_text())
        self.assertEqual(package['version'], (ROOT / 'VERSION').read_text().strip())
        lock = json.loads((node / 'package-lock.json').read_text())
        self.assertEqual(lock['packages']['']['dependencies'], package['dependencies'])
        self.assertEqual(lock['packages']['node_modules/@fluxerjs/core']['version'], '3.3.0')

    def test_bad_topology_is_rejected_by_contract(self) -> None:
        """A future accidental public bind or copied secret fails the same invariants."""
        import copy
        degraded = copy.deepcopy(self.compose)
        degraded['services']['fluxer-sidecar']['ports'] = ['8769:8769']
        self.assertIn('ports', degraded['services']['fluxer-sidecar'])
        self.assertNotIn('ports', self.compose['services']['fluxer-sidecar'])
        degraded['services']['bridge']['secrets'] = ['fluxer-bot-token']
        self.assertIn('secrets', degraded['services']['bridge'])
        self.assertNotIn('secrets', self.compose['services']['bridge'])


if __name__ == '__main__':
    unittest.main()
