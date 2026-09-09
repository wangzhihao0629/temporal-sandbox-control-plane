"""SmokeWorkflow end to end: lease a VM, run two commands, release."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.worker import Worker

from sandbox.contract.names import MANAGER_TASK_QUEUE, ORCHESTRATOR_TASK_QUEUE
from sandbox.manager.activities import ManagerActivities
from sandbox.orchestrator.workflows import SmokeParams, SmokeWorkflow
from sandbox.testing.stubs import StubProvider
from tests.integration.vmhost import InProcessVm


@pytest.fixture
async def workers(env, aws):
    registry, _ = aws
    async with (
        Worker(
            env.client,
            task_queue=MANAGER_TASK_QUEUE,
            activities=ManagerActivities(registry, StubProvider()).all(),
            activity_executor=ThreadPoolExecutor(4),
        ),
        Worker(env.client, task_queue=ORCHESTRATOR_TASK_QUEUE, workflows=[SmokeWorkflow]),
    ):
        yield


async def test_smoke_workflow_runs_two_commands_on_a_leased_vm(env, aws, workers, tmp_path):
    registry, store = aws
    vm = await InProcessVm(env.client, registry, store, tmp_path).start()
    try:
        result = await env.client.execute_workflow(
            SmokeWorkflow.run,
            SmokeParams(pool="demo", profile="test", workspace_root=str(vm.workspace_root)),
            id=f"smoke-{uuid.uuid4().hex[:8]}",
            task_queue=ORCHESTRATOR_TASK_QUEUE,
        )
        assert result.vm_id == vm.vm_id
        assert result.uname and result.whoami.startswith("uid=")
        assert result.agent_version == "test"
        assert result.lease_id
        for _ in range(50):
            if registry.get_vm(vm.vm_id)["state"] == "idle":
                break
            await asyncio.sleep(0.1)
        assert registry.get_vm(vm.vm_id)["state"] == "idle"
        assert len(registry.list_jobs(vm.vm_id)) == 2
    finally:
        await vm.stop()
