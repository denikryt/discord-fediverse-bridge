"""Check an isolated running two-container Fluxer deployment without REST writes.

Requires the real Docker engine and operator-provided Discord/Fluxer test
credentials. A sidecar restart is an explicit opt-in; no simulated transport is
accepted as deployment evidence. Captured container output may contain secrets
and is never printed, included in exceptions, or saved to a report.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time


class VerificationFailure(RuntimeError):
    """Describe a failed invariant without leaking container configuration."""


def require(condition: bool, message: str) -> None:
    """Fail on an observed deployment invariant, including under python -O."""
    if not condition:
        raise VerificationFailure(message)


def run(command: list[str], *, code: str | None = None, timeout: float = 60) -> str:
    """Capture command results privately and return only successful stdout."""
    try:
        result = subprocess.run(command, input=code, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise VerificationFailure("Docker operation unavailable or timed out") from None
    require(result.returncode == 0, "Docker operation failed; inspect the isolated deployment privately")
    return result.stdout


def inspect(container: str) -> dict:
    """Read actual engine state rather than assuming Compose YAML was applied."""
    return json.loads(run(["docker", "inspect", container]))[0]


def wait_healthy(container: str, timeout: float = 120) -> dict:
    """Wait for an actual engine healthcheck with a fixed monotonic deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = inspect(container)
        if state['State'].get('Health', {}).get('Status') == 'healthy':
            return state
        require(state['State']['Running'], "Container exited before readiness")
        time.sleep(1)
    raise VerificationFailure("Container did not become healthy")


# These probes run inside Python's real container and use its token-free config.
# No second runtime or SQLite session is created by a readiness observation.
READY_PROBE = '''
import asyncio, json, os
from pathlib import Path
from src.config import Settings
from src.fluxer_rpc_client import FluxerRpcClient
from src.fluxer_adapter import FluxerAdapter
from src.runtime_ownership import acquire_runtime_lifetime_lease
from src.profile_media import FileLock
settings = Settings()
listeners = [line.split()[1].split(':')[0] for line in Path('/proc/net/tcp').read_text().splitlines()[1:]
             if line.split()[1].split(':')[1] == '2241' and line.split()[3] == '0A']
assert listeners == ['0100007F'], 'RPC listener is not exclusive to IPv4 loopback'
assert not Path('/run/secrets/fluxer-bot-token').exists()
assert 'FLUXER_BOT_TOKEN' not in os.environ
assert len(settings.fluxer_connectors) == 1
try:
    lease = acquire_runtime_lifetime_lease(settings.database_url)
except RuntimeError:
    pass
else:
    lease.close()
    raise AssertionError('No live SQLite runtime owner')
try:
    lease = FileLock(Path(settings.deployment_state_lock_path)).acquire(exclusive=True, timeout_seconds=0)
except TimeoutError:
    pass
else:
    lease.close()
    raise AssertionError('Restore is not excluded by running runtime')
async def check():
    config = settings.fluxer_connectors[0]
    rpc = FluxerRpcClient(config)
    adapter = FluxerAdapter(connector_key=config.connector_key, instance_origin=config.origin, rpc=rpc)
    try:
        assert await adapter.refresh_readiness()
        health = await rpc.call('health', {})
        assert health['state'] == 'ready' and not health['gateway_gap']
        assert health['connector_key'] == config.connector_key
        assert health['instance_origin'] == config.origin
        assert health['bot_id']
        print(json.dumps({'bot_id': health['bot_id'], 'state': 'ready'}))
    finally:
        await rpc.close()
asyncio.run(check())
'''
OFFLINE_PROBE = '''
import asyncio
import urllib.request
from src.config import Settings
from src.fluxer_rpc_client import FluxerRpcClient
from src.fluxer_adapter import FluxerAdapter
assert urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status == 200
async def check():
    config = Settings().fluxer_connectors[0]
    rpc = FluxerRpcClient(config)
    adapter = FluxerAdapter(connector_key=config.connector_key, instance_origin=config.origin, rpc=rpc)
    try:
        assert not await adapter.refresh_readiness()
        assert not adapter.dispatch_ready()
    finally:
        await rpc.close()
asyncio.run(check())
'''
AUTH_PROBE = '''
import urllib.error, urllib.request
for url in ('http://127.0.0.1:8769/rpc', 'http://127.0.0.1:8080/internal/fluxer/events'):
    request = urllib.request.Request(url, data=b'{}', headers={'Content-Type':'application/json'})
    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as error:
        assert error.code in (401,403)
    else:
        raise AssertionError('Unauthenticated private request accepted')
'''


def verify(compose: list[str], *, restart_sidecar: bool, stop_services: bool) -> dict:
    """Observe actual namespace, mount, health, auth and optional restart behavior."""
    identifiers = {}
    states = {}
    for service in ('bridge', 'fluxer-sidecar'):
        ids = run([*compose, 'ps', '-q', service]).split()
        require(len(ids) == 1, "Expected exactly one running container per service")
        identifiers[service] = ids[0]
        states[service] = wait_healthy(ids[0])
    bridge, sidecar = identifiers['bridge'], identifiers['fluxer-sidecar']
    mode = states['fluxer-sidecar']['HostConfig']['NetworkMode']
    require(mode == f'container:{bridge}', "Sidecar does not share the live bridge network namespace")
    for state in states.values():
        require(not any(key.split('/')[0] == '8769' and value
                        for key, value in (state['HostConfig'].get('PortBindings') or {}).items()),
                "Private RPC port is published")
    mounts = states['fluxer-sidecar']['Mounts']
    require(any(mount['Destination'] == '/run/secrets/fluxer-bot-token' and not mount['RW']
                for mount in mounts), "Node bot credential is not mounted read-only")
    ready = json.loads(run(['docker', 'exec', '-i', bridge, 'python', '-'], code=READY_PROBE))
    run(['docker', 'exec', '-i', bridge, 'python', '-'], code=AUTH_PROBE)
    result = {'namespace': 'passed', 'secret_isolation': 'passed', 'authenticated_readiness': 'passed',
              'restart': 'not_run', 'discord_message_progress': 'requires_live_action',
              'ordered_full_shutdown': 'not_run', 'physical_rest_acceptance': 'not_run'}
    if restart_sidecar:
        before = states['bridge']
        run([*compose, 'stop', 'fluxer-sidecar'])
        try:
            run(['docker', 'exec', '-i', bridge, 'python', '-'], code=OFFLINE_PROBE)
        finally:
            # Restore the test sidecar even if the offline invariant fails.
            run([*compose, 'start', 'fluxer-sidecar'])
        wait_healthy(sidecar)
        after_ready = json.loads(run(['docker', 'exec', '-i', bridge, 'python', '-'], code=READY_PROBE))
        after = inspect(bridge)
        require(before['State']['StartedAt'] == after['State']['StartedAt'] and
                before['RestartCount'] == after['RestartCount'] and after['State']['Running'],
                "Python runtime restarted during the independent sidecar restart")
        require(ready['bot_id'] == after_ready['bot_id'], "Bot identity changed after restart")
        result['restart'] = 'passed'
    if stop_services:
        # Compose must stop its network-namespace borrower before the provider.
        # This is opt-in and only operates on the explicitly selected test project.
        run([*compose, 'stop', 'bridge', 'fluxer-sidecar'], timeout=150)
        stopped_bridge, stopped_node = inspect(bridge), inspect(sidecar)
        require(not stopped_bridge['State']['Running'] and not stopped_node['State']['Running'],
                "Selected services did not stop")
        require(stopped_bridge['State']['ExitCode'] == 0 and stopped_node['State']['ExitCode'] == 0,
                "Shutdown required a kill or failed resource cleanup")
        require(stopped_node['State']['FinishedAt'] <= stopped_bridge['State']['FinishedAt'],
                "Python exited before the Node namespace borrower")
        # A fresh one-shot container checks the same mounted SQLite/lease paths.
        # No replacement runtime, schema migration or physical REST write occurs.
        run([*compose, 'run', '--rm', '--no-deps', '-T', '--entrypoint', 'python', 'bridge', '-'], code="""
from pathlib import Path
from src.config import Settings
from src.profile_media import FileLock
from src.runtime_ownership import acquire_runtime_lifetime_lease
from src.db import Database
settings = Settings()
with acquire_runtime_lifetime_lease(settings.database_url):
    with FileLock(Path(settings.deployment_state_lock_path)).acquire(exclusive=True, timeout_seconds=5):
        database = Database(settings.database_url)
        try:
            with database.engine.connect() as connection:
                assert connection.exec_driver_sql('PRAGMA integrity_check').scalar() == 'ok'
        finally:
            database.close()
""")
        result['ordered_full_shutdown'] = 'passed'
    return result


def main() -> int:
    """Require an explicitly named isolated Compose project; never guess a target."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--project-name', required=True)
    parser.add_argument('--restart-sidecar', action='store_true')
    parser.add_argument('--stop-services', action='store_true', help='Stop the isolated bridge and sidecar after checking them')
    args = parser.parse_args()
    if not shutil.which('docker'):
        print('BLOCKED: Docker CLI is unavailable', file=sys.stderr)
        return 2
    if not args.env_file.is_file():
        print('BLOCKED: isolated deployment environment file is unavailable', file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    compose = ['docker', 'compose', '--env-file', str(args.env_file.resolve()),
               '--project-name', args.project_name, '--profile', 'fluxer', '-f', str(root / 'compose.yaml')]
    try:
        print(json.dumps(verify(compose, restart_sidecar=args.restart_sidecar, stop_services=args.stop_services), sort_keys=True))
    except (VerificationFailure, KeyError, ValueError):
        print('FAIL: deployment invariant or Docker operation failed; inspect the test deployment privately', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
