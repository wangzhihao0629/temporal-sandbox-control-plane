"""HoldWorkflow: holds a lease for N seconds so chaos drills have something to break."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.worker import Worker

from sandbox.contract.names import MANAGER_TASK_QUEUE, ORCHESTRATOR_TASK_QUEUE
from sandbox.manager.activities import ManagerActivities
from sandbox.orchestrator.workflows import HoldParams, HoldWorkflow
from tests.integration.vmhost import InProcessVm


@pytest.fixture
async def workers(env, aws):
    registry, _ = aws
    async with (
        Worker(
            env.client,
            task_queue=MANAGER_TASK_QUEUE,
            activities=ManagerActivities(registry, None).all(),
            activity_executor=ThreadPoolExecutor(4),
        ),
        Worker(env.client, task_queue=ORCHESTRATOR_TASK_QUEUE, workflows=[HoldWorkflow]),
    ):
        yield


async def test_hold_keeps_the_lease_until_the_sleep_ends(env, aws, workers, tmp_path):
    registry, store = aws
    vm = await InProcessVm(env.client, registry, store, tmp_path).start()
    try:
        handle = await env.client.start_workflow(
            HoldWorkflow.run,
            HoldParams(seconds=3, profile="test", workspace_root=str(vm.workspace_root)),
            id=f"hold-{uuid.uuid4().hex[:8]}",
            task_queue=ORCHESTRATOR_TASK_QUEUE,
        )
        await asyncio.sleep(1.5)
        assert registry.get_vm(vm.vm_id)["state"] == "leased"
        result = await handle.result()
        assert result.vm_id == vm.vm_id and result.held_seconds == 3
    finally:
        await vm.stop()


async def test_terminating_a_hold_leaves_the_row_leased_for_the_reconciler(
    env, aws, workers, tmp_path
):
    registry, store = aws
    vm = await InProcessVm(env.client, registry, store, tmp_path).start()
    try:
        handle = await env.client.start_workflow(
            HoldWorkflow.run,
            HoldParams(seconds=60, profile="test", workspace_root=str(vm.workspace_root)),
            id=f"hold-{uuid.uuid4().hex[:8]}",
            task_queue=ORCHESTRATOR_TASK_QUEUE,
        )
        await asyncio.sleep(1.5)
        await handle.terminate("drill")
        with pytest.raises(WorkflowFailureError):
            await handle.result()
        assert registry.get_vm(vm.vm_id)["state"] == "leased", (
            "terminate skips finally; that is the orphan the reconciler exists for"
        )
    finally:
        for job_id in vm.runtime.jobs.running_job_ids():
            vm.runtime.jobs.cancel(job_id, grace_seconds=1)
        await vm.stop()
