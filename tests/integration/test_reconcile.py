"""ReconcileWorkflow end to end: top-up from zero, dead VM, orphan, demand, scale-in, sweep."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.worker import Worker

from sandbox.contract.names import MANAGER_TASK_QUEUE
from sandbox.manager.activities import ManagerActivities
from sandbox.manager.providers.fake import FakeProvider
from sandbox.manager.reconcile import ReconcileWorkflow
from sandbox.manager.reconcile_activities import ReconcileActivities
from sandbox.manager.reconcile_types import ReconcileParams
from sandbox.manager.reconciler import Reconciler, Tunables
from sandbox.manager.schedule import ensure_schedule


@pytest.fixture
async def fleet(env, aws, tmp_path):
    registry, store = aws
    registry.ensure_policy("demo", min_idle=2, max=3, image="fake")
    provider = FakeProvider(asyncio.get_running_loop(), env.client, registry, store, tmp_path)
    reconciler = Reconciler(registry, provider, Tunables.test())
    async with Worker(
        env.client,
        task_queue=MANAGER_TASK_QUEUE,
        workflows=[ReconcileWorkflow],
        activities=[
            *ManagerActivities(registry, provider).all(),
            *ReconcileActivities(reconciler, env.client).all(),
        ],
        activity_executor=ThreadPoolExecutor(8),
    ):
        yield registry, provider
    for vm_id in list(provider.vms):
        await asyncio.to_thread(provider.terminate, vm_id)


async def _reconcile(env, n=1):
    report = None
    for i in range(n):
        report = await env.client.execute_workflow(
            ReconcileWorkflow.run,
            ReconcileParams(pool="demo", profile="test"),
            id=f"reconcile-test-{asyncio.get_running_loop().time():.3f}-{i}",
            task_queue=MANAGER_TASK_QUEUE,
        )
    return report


async def _wait(predicate, seconds=10):
    for _ in range(int(seconds / 0.2)):
        if predicate():
            return
        await asyncio.sleep(0.2)
    raise AssertionError("condition not met in time")


def _states(registry):
    return sorted(r["state"] for r in registry.list_vms("demo"))


async def test_top_up_from_zero_then_steady_state(env, fleet):
    registry, provider = fleet
    report = await _reconcile(env)
    assert [a.kind for a in report.actions] == ["launch", "launch"]
    await _wait(lambda: _states(registry) == ["idle", "idle"])
    report = await _reconcile(env)
    assert report.actions == []
    assert report.counts["idle"] == 2 and report.counts["total"] == 2
    assert registry.latest_fleet_sample()["details"]["idle"] == 2


async def test_a_crashed_vm_is_written_off_and_replaced(env, fleet):
    registry, provider = fleet
    await _reconcile(env)
    await _wait(lambda: _states(registry) == ["idle", "idle"])
    victim = provider.launched[0]
    await asyncio.to_thread(provider.kill, victim)
    report = await _reconcile(env)
    kinds = [a.kind for a in report.actions]
    assert "write_off_missing" in kinds and kinds.count("launch") == 1
    assert registry.get_vm(victim)["state"] == "terminated"
    await _wait(lambda: _states(registry).count("idle") == 2)
    await asyncio.sleep(Tunables.test().sweep_after_seconds + 0.5)
    report = await _reconcile(env)
    assert "sweep" in [a.kind for a in report.actions]
    assert registry.get_vm(victim) is None


async def test_an_orphaned_lease_is_released_and_the_vm_recycled(env, fleet):
    registry, provider = fleet
    await _reconcile(env)
    await _wait(lambda: _states(registry) == ["idle", "idle"])
    row = registry.claim_idle(
        "demo",
        lease_id="orphan",
        request_id="r-orphan",
        owner_workflow_id="never-existed",
        owner_run_id="",
        hold_seconds=600,
        contract_major=1,
        labels={},
    )
    report = await _reconcile(env)
    assert [a for a in report.actions if a.kind == "orphan"][0].vm_id == row["vm_id"]
    await _wait(lambda: registry.get_vm(row["vm_id"])["state"] == "idle")


async def test_a_running_owner_keeps_its_lease(env, fleet):
    registry, provider = fleet
    await _reconcile(env)
    await _wait(lambda: _states(registry) == ["idle", "idle"])
    handle = await env.client.start_workflow(
        ReconcileWorkflow.run,
        ReconcileParams(pool="demo", profile="test"),
        id="reconcile-owner-probe",
        task_queue=MANAGER_TASK_QUEUE,
    )
    # Use this running workflow as a stand-in owner: claim a row in its name.
    row = registry.claim_idle(
        "demo",
        lease_id="held",
        request_id="r-held",
        owner_workflow_id="reconcile-owner-probe",
        owner_run_id=handle.result_run_id or "",
        hold_seconds=600,
        contract_major=1,
        labels={},
    )
    await handle.result()
    # The probe has now finished, so the lease it "held" is orphaned on the next pass.
    report = await _reconcile(env)
    assert any(a.kind == "orphan" and a.vm_id == row["vm_id"] for a in report.actions)


async def test_pending_demand_launches_and_surplus_scales_in(env, fleet):
    registry, provider = fleet
    await _reconcile(env)
    await _wait(lambda: _states(registry) == ["idle", "idle"])
    registry.record_pending("r-demand", "demo", "wf-demand")
    report = await _reconcile(env)
    assert [a.kind for a in report.actions] == ["launch"]
    await _wait(lambda: _states(registry) == ["idle", "idle", "idle"])
    registry.fulfill_request("r-demand")
    await asyncio.sleep(Tunables.test().scale_in_cooldown_seconds + 0.5)
    report = await _reconcile(env)
    assert [a.kind for a in report.actions] == ["scale_in"]
    await _wait(lambda: _states(registry).count("idle") == 2)


async def test_stale_pending_requests_are_abandoned(env, fleet):
    registry, provider = fleet
    registry.record_pending("r-old", "demo", "wf-old")
    await asyncio.sleep(Tunables.test().abandon_after_seconds + 0.5)
    report = await _reconcile(env)
    assert "abandon" in [a.kind for a in report.actions]
    assert registry.pending_count("demo") == 0


async def test_ensure_schedule_is_idempotent(env):
    schedule_id = await ensure_schedule(env.client, "demo", 30, "test")
    assert schedule_id == "sandbox-reconcile-demo"
    assert await ensure_schedule(env.client, "demo", 45, "test") == schedule_id
    handle = env.client.get_schedule_handle(schedule_id)
    desc = await handle.describe()
    assert desc.schedule.spec.intervals[0].every.total_seconds() == 45
    await handle.delete()
