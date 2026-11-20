"""Shared test fixtures remain importable by spawned Python interpreters."""
import multiprocessing


def _import_fixtures(channel):
    """Import the unique fixture package without a pytest session in the child."""
    import yaml
    from bridge_test_support.physical_mutation_recovery import PhysicalMutationScenario
    channel.send((PhysicalMutationScenario.__module__, yaml.safe_load("ready: true")))
    channel.close()


def test_spawn_imports_shared_fixtures_without_vendor_tests_collision():
    """Spawn reproduces the import boundary used by real lock ownership tests."""
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_import_fixtures, args=(child,))
    process.start()
    child.close()
    try:
        assert parent.poll(10)
        assert parent.recv() == (
            "bridge_test_support.physical_mutation_recovery", {"ready": True}
        )
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
