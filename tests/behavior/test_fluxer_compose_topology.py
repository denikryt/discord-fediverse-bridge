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
        self.assertIn('if [[ -f fluxer-connector/package-lock.json ]]', workflow)
        self.assertIn("needs.validate-version.outputs.fluxer_lock == 'true'", workflow)
        self.assertNotIn('hashFiles(', workflow)
        self.assertIn('file: fluxer-connector/Dockerfile', workflow)
        import json
        package = json.loads((node / 'package.json').read_text())
        self.assertEqual(package['version'], (ROOT / 'VERSION').read_text().strip())
        self.assertNotIn('package-lock.json', [p.name for p in node.iterdir()])

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
