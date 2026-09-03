"""Manager activities: acquire idempotency and capacity, release dispositions."""

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from sandbox.contract.types import ReleaseRequest, SandboxSpec
from sandbox.manager.activities import ManagerActivities


class FakeProvider:
    def __init__(self):
        self.terminated: list[str] = []

    def terminate(self, ref):
        self.terminated.append(ref)


def _idle(registry, vm_id):
    registry.register_vm(vm_id, "demo", vm_id, "test", [1], {})
    assert registry.set_state(vm_id, "idle", expect="booting")


async def test_acquire_without_capacity_raises_no_capacity_and_records_the_request(registry):
    acts = ManagerActivities(registry)
    with pytest.raises(ApplicationError) as err:
        ActivityEnvironment().run(acts.acquire, SandboxSpec(pool="demo", request_id="r1"))
    assert err.value.type == "NoCapacity"
    assert err.value.non_retryable is True
    assert registry.pending_count("demo") == 1


async def test_acquire_claims_and_is_idempotent_on_request_id(registry):
    _idle(registry, "sbx-a")
    acts = ManagerActivities(registry)
    env = ActivityEnvironment()
    lease = env.run(acts.acquire, SandboxSpec(pool="demo", request_id="r1"))
    assert lease.vm_id == "sbx-a" and lease.task_queue == "sandbox-vm-sbx-a"
    again = env.run(acts.acquire, SandboxSpec(pool="demo", request_id="r1"))
    assert again == lease
    assert registry.pending_count("demo") == 0
    assert registry.get_vm("sbx-a")["owner_workflow_id"] == env.info.workflow_id


async def test_acquire_rejects_unknown_contract_major(registry):
    _idle(registry, "sbx-a")
    acts = ManagerActivities(registry)
    with pytest.raises(ApplicationError) as err:
        ActivityEnvironment().run(
            acts.acquire, SandboxSpec(pool="demo", request_id="r1", contract_major=99)
        )
    assert err.value.type == "Incompatible"


async def test_release_recycles_idempotently(registry):
    _idle(registry, "sbx-a")
    acts = ManagerActivities(registry)
    env = ActivityEnvironment()
    lease = env.run(acts.acquire, SandboxSpec(pool="demo", request_id="r1"))
    env.run(acts.release, ReleaseRequest(vm_id=lease.vm_id, lease_id=lease.lease_id))
    env.run(acts.release, ReleaseRequest(vm_id=lease.vm_id, lease_id=lease.lease_id))
    row = registry.get_vm("sbx-a")
    assert row["state"] == "recycling" and "lease_id" not in row
    assert [e["type"] for e in registry.recent_events(10)][:2] == ["release", "acquire"]


async def test_release_destroy_terminates_through_the_provider(registry):
    _idle(registry, "sbx-a")
    provider = FakeProvider()
    acts = ManagerActivities(registry, provider)
    env = ActivityEnvironment()
    lease = env.run(acts.acquire, SandboxSpec(pool="demo", request_id="r1"))
    env.run(
        acts.release,
        ReleaseRequest(vm_id=lease.vm_id, lease_id=lease.lease_id, disposition="destroy"),
    )
    assert provider.terminated == ["sbx-a"]
    assert registry.get_vm("sbx-a")["state"] == "terminated"
