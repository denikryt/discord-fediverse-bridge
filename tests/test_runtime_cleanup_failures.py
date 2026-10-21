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

        async def _run(self, step):
            steps.append(step)
            if step == failure:
                raise RuntimeError(f'{step} failure')

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


@pytest.mark.asyncio
async def test_runtime_close_reports_primary_and_secondary_failures_in_order():
    """A failing lease cleanup cannot overwrite the earlier worker-stop failure."""
    steps = []

    class Owner:
        """Simulate two independent failures amid the production shutdown order."""

        def __init__(self, step):
            """Keep the named lifecycle phase for assertions."""
            self.step = step

        def close_ingress_admission(self):
            """Close the admission gate even when later steps fail."""
            steps.append('admission')

        async def drain_ingress(self):
            """Drain accepted callbacks before either worker."""
            steps.append('ingress')

        async def stop(self):
            """Fail exactly the first worker after its join completes."""
            steps.append(self.step)
            if self.step == 'feed':
                raise RuntimeError('first feed failure')

        async def quiesce(self):
            """Quiesce SDK I/O after worker stop attempts."""
            steps.append('io')

        async def close(self):
            """Close an async owned client."""
            steps.append(self.step)

    class SyncOwner(Owner):
        """Exercise synchronous DB and lease release boundaries."""

        def close(self):
            """Raise on the last cleanup to test preservation of the first error."""
            steps.append(self.step)
            if self.step == 'lease':
                raise RuntimeError('final lease failure')

    world = SimpleNamespace(
        discord_adapter=Owner('adapter'),
        personal_feed_discord_delivery_worker=Owner('feed'),
        user_actor_delivery_worker=Owner('user'),
        discord_oauth_client=Owner('oauth'), fedify_gateway=Owner('fedify'),
        database=SyncOwner('database'), deployment_state_lease=SyncOwner('lease'),
    )
    with pytest.raises(ExceptionGroup) as raised:
        await _close_method()(world)
    assert [str(error) for error in raised.value.exceptions] == [
        'first feed failure', 'final lease failure',
    ]
    assert steps == ['admission', 'ingress', 'feed', 'user', 'io', 'oauth', 'fedify', 'database', 'lease']


@pytest.mark.asyncio
async def test_runtime_close_retains_cancellation_and_secondary_error():
    """Cancellation during shutdown is not replaced by a late release failure."""
    steps = []

    class Owner:
        """Represent one named close owner with deterministic async failure."""

        def __init__(self, step):
            """Remember the owning step for the shutdown ordering assertions."""
            self.step = step

        def close_ingress_admission(self):
            """Close callback admission before any awaited operation."""
            steps.append('admission')

        async def drain_ingress(self):
            """Complete callback draining."""
            steps.append('ingress')

        async def stop(self):
            """Model a cancelled stop after the worker has been joined."""
            steps.append(self.step)
            if self.step == 'feed':
                raise asyncio.CancelledError()

        async def quiesce(self):
            """Complete SDK I/O teardown."""
            steps.append('io')

        async def close(self):
            """Complete client teardown."""
            steps.append(self.step)

    class SyncOwner(Owner):
        """Represent database and deployment lease release."""

        def close(self):
            """Inject a later cleanup error without overwriting cancellation."""
            steps.append(self.step)
            if self.step == 'lease':
                raise RuntimeError('lease failure')

    world = SimpleNamespace(
        discord_adapter=Owner('adapter'),
        personal_feed_discord_delivery_worker=Owner('feed'),
        user_actor_delivery_worker=Owner('user'),
        discord_oauth_client=Owner('oauth'), fedify_gateway=Owner('fedify'),
        database=SyncOwner('database'), deployment_state_lease=SyncOwner('lease'),
    )
    with pytest.raises(BaseExceptionGroup) as raised:
        await _close_method()(world)
    assert isinstance(raised.value.exceptions[0], asyncio.CancelledError)
    assert str(raised.value.exceptions[1]) == 'lease failure'
    assert steps == ['admission', 'ingress', 'feed', 'user', 'io', 'oauth', 'fedify', 'database', 'lease']
