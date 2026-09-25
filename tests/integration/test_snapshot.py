"""Snapshot and restore through Temporal, between two VMs, the way a workflow calls them."""

import uuid

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.worker import Worker

from tests.integration.vmhost import InProcessVm
from tests.integration.workflows import SnapshotParams, SnapshotRestoreWorkflow

URI = "s3://sandbox-sessions/snap-test/snapshots/repo.tar.gz"


@pytest.fixture
async def two_vms(env, aws, tmp_path):
    registry, store = aws
    first = await InProcessVm(env.client, registry, store, tmp_path / "a").start()
    second = await InProcessVm(env.client, registry, store, tmp_path / "b").start()
    yield first, second
    await first.stop()
    await second.stop()


@pytest.fixture
async def orchestrator(env):
    async with Worker(env.client, task_queue="test-snap", workflows=[SnapshotRestoreWorkflow]):
        yield "test-snap"


async def _run(env, queue, params):
    return await env.client.execute_workflow(
        SnapshotRestoreWorkflow.run, params, id=f"snap-{uuid.uuid4().hex[:8]}", task_queue=queue
    )


async def test_a_directory_snapshotted_on_one_vm_is_restored_on_another(env, two_vms, orchestrator):
    first, second = two_vms
    src = first.workspace_root / "wf" / "repo"
    (src / "bin").mkdir(parents=True)
    (src / "main.go").write_text("package main\n")
    (src / "bin" / "app").write_text("#!/bin/sh\necho restored\n")
    dst = second.workspace_root / "wf" / "repo"
    ref = await _run(
        env,
        orchestrator,
        SnapshotParams(first.task_queue, second.task_queue, str(src), str(dst), URI),
    )
    assert ref.files == 3 and ref.path == str(dst)
    assert (dst / "main.go").read_text() == "package main\n"
    assert (dst / "bin" / "app").read_text() == "#!/bin/sh\necho restored\n"


async def test_a_path_outside_the_workspace_is_refused(env, two_vms, orchestrator, tmp_path):
    first, second = two_vms
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(WorkflowFailureError):
        await _run(
            env,
            orchestrator,
            SnapshotParams(
                first.task_queue,
                second.task_queue,
                str(outside),
                str(second.workspace_root / "x"),
                URI,
            ),
        )
