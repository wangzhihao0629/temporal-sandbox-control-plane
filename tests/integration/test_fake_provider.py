"""FakeProvider: in-process VMs appear in the registry and vanish the way real ones do."""

import asyncio

import pytest

from sandbox.manager.providers.base import LaunchSpec
from sandbox.manager.providers.fake import FakeProvider


@pytest.fixture
async def provider(env, aws, tmp_path):
    registry, store = aws
    return FakeProvider(asyncio.get_running_loop(), env.client, registry, store, tmp_path)


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
    assert provider.list() == [] and provider.describe("sbx-fake1") is None
    assert registry.get_vm("sbx-fake1")["state"] == "terminated"


async def test_kill_leaves_the_row_behind_like_a_real_crash(provider, aws):
    registry, _ = aws
    await asyncio.to_thread(provider.launch, "sbx-fake2", LaunchSpec(image="x"), {})
    await _settle(registry, "sbx-fake2", "idle")
    await asyncio.to_thread(provider.kill, "sbx-fake2")
    assert provider.list() == []
    assert registry.get_vm("sbx-fake2")["state"] == "idle", "a crash writes nothing"


async def test_stop_drains_and_the_instance_disappears(provider, aws):
    registry, _ = aws
    await asyncio.to_thread(provider.launch, "sbx-fake3", LaunchSpec(image="x"), {})
    await _settle(registry, "sbx-fake3", "idle")
    await asyncio.to_thread(provider.stop, "sbx-fake3")
    assert provider.list() == []
    assert registry.get_vm("sbx-fake3")["state"] == "terminated"
