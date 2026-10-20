"""Runtime shutdown attempts every owner even when an earlier owner fails."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


def _close_method():
    """Execute the actual Runtime.close method without requiring Discord SDK imports."""
    source = Path(__file__).resolve().parents[1] / 'src/runtime.py'
    module = ast.parse(source.read_text())
    runtime = next(item for item in module.body if isinstance(item, ast.ClassDef) and item.name == 'Runtime')
    method = next(item for item in runtime.body if isinstance(item, ast.AsyncFunctionDef) and item.name == 'close')
    method.decorator_list = []
    scope = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), scope)
    return scope['close']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['ingress', 'feed', 'user', 'io', 'oauth', 'fedify', 'database'])
async def test_runtime_close_attempts_all_resources_after_one_failure(failure):
    """Workers, SDK, Fedify, DB and lease all receive cleanup despite one error."""
    steps = []

    class Owner:
        """Record the lifecycle boundaries of a real Runtime.close call."""

        def __init__(self, name):
            self.name = name

        async def _run(self, stage):
            steps.append(stage)
            if stage == failure:
                raise RuntimeError(f'{stage} failure')

        def close_ingress_admission(self):
            steps.append('admission')

        async def drain_ingress(self):
            await self._run('ingress')

        async def stop(self):
            await self._run(self.name)

        async def quiesce(self):
            await self._run('io')

        async def close(self):
            await self._run(self.name)

    class SyncOwner(Owner):
        """Record synchronous DB close without inventing async DB cleanup."""

        def close(self):
            steps.append(self.name)
            if self.name == failure:
                raise RuntimeError(f'{self.name} failure')

    world = SimpleNamespace(
        discord_adapter=Owner('adapter'),
        personal_feed_discord_delivery_worker=Owner('feed'),
        user_actor_delivery_worker=Owner('user'),
        discord_oauth_client=Owner('oauth'),
        fedify_gateway=Owner('fedify'),
        database=SyncOwner('database'),
        deployment_state_lease=SyncOwner('lease'),
    )
    with pytest.raises(RuntimeError, match=f'{failure} failure'):
        await _close_method()(world)
    assert steps == ['admission', 'ingress', 'feed', 'user', 'io', 'oauth', 'fedify', 'database', 'lease']


@pytest.mark.asyncio
async def test_runtime_close_normal_completion_releases_every_owner():
    """The no-error path still closes all resources in lifetime order."""
    steps = []

    class Owner:
        """Record an ordinary asynchronous service shutdown."""

        def __init__(self, name):
            self.name = name

        def close_ingress_admission(self):
            steps.append('admission')

        async def drain_ingress(self):
            steps.append('ingress')

        async def stop(self):
            steps.append(self.name)

        async def quiesce(self):
            steps.append('io')

        async def close(self):
            steps.append(self.name)

    class SyncOwner(Owner):
        """Use immediate DB and lease closures like production."""

        def close(self):
            steps.append(self.name)

    world = SimpleNamespace(
        discord_adapter=Owner('adapter'),
        personal_feed_discord_delivery_worker=Owner('feed'),
        user_actor_delivery_worker=Owner('user'),
        discord_oauth_client=Owner('oauth'),
        fedify_gateway=Owner('fedify'),
        database=SyncOwner('database'),
        deployment_state_lease=SyncOwner('lease'),
    )
    await _close_method()(world)
    assert steps == ['admission', 'ingress', 'feed', 'user', 'io', 'oauth', 'fedify', 'database', 'lease']
