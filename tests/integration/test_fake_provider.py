"""FakeProvider: in-process VMs appear in the registry and vanish the way real ones do."""

import asyncio

import pytest

from sandbox.manager.providers.base import LaunchSpec
from sandbox.manager.providers.fake import FakeProvider


@pytest.fixture
async def provider(env, aws, tmp_path):
    registry, store = aws
    provider = FakeProvider(asyncio.get_running_loop(), env.client, registry, store, tmp_path)
    yield provider
    for provider_ref in [*provider.vms, *provider.stopped]:
        await asyncio.to_thread(provider.terminate, provider_ref)


async def _settle(registry, vm_id, state, seconds=5):
    for _ in range(int(seconds / 0.1)):
        row = registry.get_vm(vm_id)
        if row and row["state"] == state:
            return row
        await asyncio.sleep(0.1)
    raise AssertionError(f"{vm_id} never reached {state}")


async def test_launch_registers_and_terminate_removes(provider, aws):
    registry, _ = aws
    ref = await asyncio.to_thread(provider.launch, "sbx-fake1", LaunchSpec(image="x"), {})
    assert ref == "sbx-fake1"
    await _settle(registry, "sbx-fake1", "idle")
    assert [i.vm_id for i in provider.list()] == ["sbx-fake1"]
    assert provider.describe("sbx-fake1").state == "running"
    await asyncio.to_thread(provider.terminate, "sbx-fake1")
    assert provider.list() == []
    assert provider.describe("sbx-fake1") is None
    assert registry.get_vm("sbx-fake1")["state"] == "terminated"


async def test_kill_leaves_the_row_behind_like_a_real_crash(provider, aws):
    registry, _ = aws
    await asyncio.to_thread(provider.launch, "sbx-fake2", LaunchSpec(image="x"), {})
    await _settle(registry, "sbx-fake2", "idle")
    await asyncio.to_thread(provider.kill, "sbx-fake2")
    # The instance is still there, like a crashed EC2 instance: stopped, not gone.
    assert [(i.vm_id, i.state) for i in provider.list()] == [("sbx-fake2", "stopped")]
    assert provider.describe("sbx-fake2").state == "stopped"
    assert registry.get_vm("sbx-fake2")["state"] == "idle", "a crash writes nothing"
    # A heartbeat already in flight when the crash landed may still write one row,
    # so the baseline is read after it, not before.
    await asyncio.sleep(0.1)
    beat = registry.get_vm("sbx-fake2")["last_heartbeat_at"]
    # Two heartbeat intervals with nothing left alive to write one.
    await asyncio.sleep(1.2)
    assert registry.get_vm("sbx-fake2")["last_heartbeat_at"] == beat
    await asyncio.to_thread(provider.terminate, "sbx-fake2")
    assert provider.list() == [] and provider.describe("sbx-fake2") is None


async def test_stop_drains_and_the_instance_stays_until_terminated(provider, aws):
    registry, _ = aws
    await asyncio.to_thread(provider.launch, "sbx-fake3", LaunchSpec(image="x"), {})
    await _settle(registry, "sbx-fake3", "idle")
    await asyncio.to_thread(provider.stop, "sbx-fake3")
    assert [(i.vm_id, i.state) for i in provider.list()] == [("sbx-fake3", "stopped")]
    assert registry.get_vm("sbx-fake3")["state"] == "terminated"
    await asyncio.to_thread(provider.terminate, "sbx-fake3")
    assert provider.list() == []
