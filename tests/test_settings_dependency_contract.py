"""Deployment dependency contract for the one-pass platform selector source."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINNED_SETTINGS = '2.14.1'


class SettingsDependencyContract(unittest.TestCase):
    """Keep Docker's pip resolver and uv's locked source-hook ABI aligned."""

    def test_python_image_and_uv_resolve_the_verified_settings_source_abi(self) -> None:
        """A pip-built image must not select a version lacking our source hook."""
        # Pydantic Settings 2.8.0 supports _settings_build_values, but not the
        # _settings_init_sources hook used to install the single-pass dotenv
        # source. The image installs from project metadata instead of uv.lock.
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
        dependency = [item for item in project['project']['dependencies']
                      if item.startswith('pydantic-settings')]
        self.assertEqual(dependency, [f'pydantic-settings=={PINNED_SETTINGS}'])

        lock = tomllib.loads((ROOT / 'uv.lock').read_text(encoding='utf-8'))
        locked_package = [package for package in lock['package']
                          if package['name'] == 'pydantic-settings']
        self.assertEqual([package['version'] for package in locked_package],
                         [PINNED_SETTINGS])
        bridge = next(package for package in lock['package']
                      if package['name'] == project['project']['name'])
        requested = [entry for entry in bridge['metadata']['requires-dist']
                     if entry['name'] == 'pydantic-settings']
        self.assertEqual(requested, [{'name': 'pydantic-settings',
                                      'specifier': f'=={PINNED_SETTINGS}'}])
        dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
        # Docker's `pip install .` honors the exact requirement; it does not
        # fall back to the wider dependency range from older deployments.
        self.assertIn('pip install --no-cache-dir . ./vendor/discordops', dockerfile)


if __name__ == '__main__':
    unittest.main()
