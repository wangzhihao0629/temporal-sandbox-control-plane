"""The workflow-facing client end to end: lease, exec, failure translation, release."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.worker import Worker

from sandbox.contract.names import MANAGER_TASK_QUEUE
from sandbox.manager.activities import ManagerActivities
from tests.integration.client_workflows import ExerciseParams, ExerciseResult, ExerciseWorkflow
from tests.integration.vmhost import InProcessVm


@pytest.fixture
async def manager(env, aws):
    registry, _ = aws
    async with Worker(
        env.client,
        task_queue=MANAGER_TASK_QUEUE,
        activities=ManagerActivities(registry).all(),
        activity_executor=ThreadPoolExecutor(4),
    ):
        yield


@pytest.fixture
async def orchestrator(env):
    # A bare RuntimeError from workflow code is a workflow *task* failure by
    # default, retried forever; the raise_inside scenario wants the workflow to
    # fail so the test can see the release that the context manager still ran.
    async with Worker(
        env.client,
        task_queue="test-client-orch",
        workflows=[ExerciseWorkflow],
        workflow_failure_exception_types=[Exception],
    ):
        yield "test-client-orch"


async def _run(env, queue, scenario, **kw) -> ExerciseResult:
    return await env.client.execute_workflow(
        ExerciseWorkflow.run,
        ExerciseParams(scenario=scenario, **kw),
        id=f"c-{scenario}-{uuid.uuid4().hex[:8]}",
        task_queue=queue,
    )


async def _wait_state(registry, vm_id, state, seconds=5):
    for _ in range(int(seconds / 0.1)):
        if registry.get_vm(vm_id)["state"] == state:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{vm_id} never reached {state}: {registry.get_vm(vm_id)['state']}")


async def test_lease_exec_release_round_trip(env, aws, manager, orchestrator, tmp_path):
    registry, store = aws
    vm = await InProcessVm(env.client, registry, store, tmp_path).start()
    try:
        result = await _run(env, orchestrator, "echo", workspace_root=str(vm.workspace_root))
        assert result.outcome == "ok" and result.stdout == "hi\n" and result.vm_id == vm.vm_id
        await _wait_state(registry, vm.vm_id, "idle")
        assert not any(vm.workspace_root.iterdir())
        types = [e["type"] for e in registry.recent_events(20)]
        assert types[:3] == ["wipe", "release", "acquire"]
    finally:
        await vm.stop()


async def test_non_zero_exit_is_exec_failed_not_lease_lost(
    env, aws, manager, orchestrator, tmp_path
):
    registry, store = aws
    vm = await InProcessVm(env.client, registry, store, tmp_path).start()
    try:
        result = await _run(
            env, orchestrator, "failing", workspace_root=str(vm.workspace_root)
        )
        assert result.outcome == "exec_failed" and result.detail == "exit=2 bad"
    finally:
        await vm.stop()


async def test_dead_vm_surfaces_as_lease_lost_and_is_still_released(
    env, aws, manager, orchestrator
):
    registry, _ = aws
    registry.register_vm("sbx-ghost", "demo", "sbx-ghost", "test", [1], {})
    assert registry.set_state("sbx-ghost", "idle", expect="booting")
    result = await _run(env, orchestrator, "lost")
    assert result.outcome == "lease_lost"
    row = registry.get_vm("sbx-ghost")
    assert row["state"] == "recycling" and "lease_id" not in row


async def test_no_capacity_becomes_sandbox_unavailable_after_the_wait(
    env, aws, manager, orchestrator
):
    registry, _ = aws
    result = await _run(env, orchestrator, "echo")
    assert result.outcome == "unavailable"
    assert registry.pending_count("demo") == 1


async def test_exception_inside_the_context_manager_still_releases(
    env, aws, manager, orchestrator, tmp_path
):
    registry, store = aws
    vm = await InProcessVm(env.client, registry, store, tmp_path).start()
    try:
        with pytest.raises(WorkflowFailureError):
            await _run(
                env, orchestrator, "raise_inside", workspace_root=str(vm.workspace_root)
            )
        await _wait_state(registry, vm.vm_id, "idle")
    finally:
        await vm.stop()


async def test_wait_reattaches_after_the_vm_worker_restarts(
    env, aws, manager, orchestrator, tmp_path
):
    registry, store = aws
    vm = await InProcessVm(env.client, registry, store, tmp_path).start()
    try:
        handle = await env.client.start_workflow(
            ExerciseWorkflow.run,
            ExerciseParams(
                scenario="long", sleep_seconds=8, workspace_root=str(vm.workspace_root)
            ),
            id=f"c-long-{uuid.uuid4().hex[:8]}",
            task_queue=orchestrator,
        )
        await asyncio.sleep(2)
        await vm.restart_worker()
        result = await handle.result()
        assert result.outcome == "ok" and result.stdout == "done\n"
        jobs = registry.list_jobs(vm.vm_id)
        assert len(jobs) == 1 and jobs[0]["status"] == "exited"
    finally:
        await vm.stop()
