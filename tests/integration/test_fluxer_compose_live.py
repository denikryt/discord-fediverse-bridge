"""Opt-in actual-container checks; no HTTP fake substitutes for a Compose run."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_actual_compose_namespace_secret_and_independent_restart():
    """Use operator-selected isolated containers and their real SDK readiness."""
    env_file = os.environ.get('FLUXER_COMPOSE_ACCEPTANCE_ENV')
    project = os.environ.get('FLUXER_COMPOSE_ACCEPTANCE_PROJECT')
    if not env_file or not project:
        pytest.skip('Real Docker/SDK deployment requires explicit isolated env file and project')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([
        sys.executable, str(root / 'dev/verify_fluxer_deployment.py'),
        '--env-file', env_file, '--project-name', project, '--restart-sidecar', '--stop-services',
    ], capture_output=True, text=True, timeout=360)
    # The verifier deliberately prints only invariant results, never raw
    # docker inspect, environment files or unfiltered container diagnostics.
    assert result.returncode == 0, result.stdout + result.stderr
