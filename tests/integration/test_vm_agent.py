"""The VM agent end to end: boot, exec, files, artifacts, describe, wipe, cancel, drain."""

import asyncio
import hashlib
import io
import tarfile
import time
import uuid

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.worker import Worker

from tests.integration.vmhost import InProcessVm
from tests.integration.workflows import (
    ArtifactParams,
    ArtifactWorkflow,
    DescribeWorkflow,
    FileParams,
    FileRoundTripWorkflow,
    RunCommandParams,
    RunCommandWorkflow,
)

WORKFLOWS = [RunCommandWorkflow, FileRoundTripWorkflow, ArtifactWorkflow, DescribeWorkflow]


@pytest.fixture
async def vm(env, aws, tmp_path):
    registry, store = aws
    vm = await InProcessVm(env.client, registry, store, tmp_path).start()
    yield vm
    await vm.stop()


@pytest.fixture
async def orchestrator(env):
    async with Worker(env.client, task_queue="test-orch", workflows=WORKFLOWS):
        yield "test-orch"


def _ws(vm, name="wf"):
    return str(vm.workspace_root / name)


async def _run(env, orchestrator, wf, arg):
    return await env.client.execute_workflow(
        wf.run, arg, id=f"t-{uuid.uuid4().hex[:8]}", task_queue=orchestrator
    )


async def test_boot_registers_then_goes_idle(vm, aws):
    registry, _ = aws
    row = registry.get_vm(vm.vm_id)
    assert row["state"] == "idle" and row["contract_majors"] == [1]


async def test_exec_runs_a_command_and_uploads_logs(env, aws, vm, orchestrator):
    registry, store = aws
    log_uri = f"s3://sandbox-jobs/{vm.vm_id}/first"
    result = await _run(
        env,
        orchestrator,
        RunCommandWorkflow,
        RunCommandParams(
            task_queue=vm.task_queue,
            cwd=_ws(vm),
            argv=["sh", "-c", "echo hello $PROMPT; echo $GITHUB_TOKEN"],
            env={"PROMPT": "world"},
            secrets=["github_token"],
            log_uri=log_uri,
        ),
    )
    assert result.status == "exited" and result.exit_code == 0
    assert result.stdout_tail == "hello world\ndummy-github-token\n"
    assert store.get_bytes(f"{log_uri}/stdout.log") == b"hello world\ndummy-github-token\n"
    jobs = registry.list_jobs(vm.vm_id)
    assert jobs[0]["status"] == "exited" and jobs[0]["exit_code"] == 0


async def test_exec_start_is_idempotent(env, aws, vm, orchestrator):
    result = await _run(
        env,
        orchestrator,
        RunCommandWorkflow,
        RunCommandParams(
            task_queue=vm.task_queue,
            cwd=_ws(vm),
            argv=["echo", "once"],
            start_twice=True,
        ),
    )
    assert result.stdout_tail == "once\n"


async def test_exec_times_out(env, aws, vm, orchestrator):
    result = await _run(
        env,
        orchestrator,
        RunCommandWorkflow,
        RunCommandParams(
            task_queue=vm.task_queue,
            cwd=_ws(vm),
            argv=["sleep", "30"],
            timeout_seconds=2,
        ),
    )
    assert result.status == "timed_out" and result.exit_code in (None, -1)


def _assert_incompatible(err: pytest.ExceptionInfo) -> None:
    assert isinstance(err.value.cause, ActivityError)
    assert isinstance(err.value.cause.cause, ApplicationError)
    assert err.value.cause.cause.type == "Incompatible"


async def test_exec_rejects_credential_env_and_bad_cwd(env, aws, vm, orchestrator, tmp_path):
    with pytest.raises(WorkflowFailureError) as err:
        await _run(
            env,
            orchestrator,
            RunCommandWorkflow,
            RunCommandParams(
                task_queue=vm.task_queue,
                cwd=_ws(vm),
                argv=["true"],
                env={"MY_TOKEN": "x"},
            ),
        )
    _assert_incompatible(err)

    # tmp_path is the parent of this VM's workspace root, so it is a real
    # directory the agent could write to and must still refuse.
    with pytest.raises(WorkflowFailureError) as err:
        await _run(
            env,
            orchestrator,
            RunCommandWorkflow,
            RunCommandParams(task_queue=vm.task_queue, cwd=str(tmp_path), argv=["true"]),
        )
    _assert_incompatible(err)


async def test_file_round_trip(env, aws, vm, orchestrator):
    _, store = aws
    store.put_bytes("s3://sandbox-out/in.txt", b"payload")
    stat = await _run(
        env,
        orchestrator,
        FileRoundTripWorkflow,
        FileParams(
            task_queue=vm.task_queue,
            src_uri="s3://sandbox-out/in.txt",
            remote_path=str(vm.workspace_root / "wf" / "in.txt"),
            dst_uri="s3://sandbox-out/back.txt",
        ),
    )
    assert stat.size == 7 and store.get_bytes("s3://sandbox-out/back.txt") == b"payload"


async def test_ensure_artifact_extracts_and_caches(env, aws, vm, orchestrator):
    _, store = aws
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"print('hi')\n"
        info = tarfile.TarInfo("runner/main.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    digest = hashlib.sha256(blob).hexdigest()
    store.put_bytes("s3://sandbox-artifacts/runner.tar.gz", blob)
    ref = await _run(
        env,
        orchestrator,
        ArtifactWorkflow,
        ArtifactParams(
            task_queue=vm.task_queue,
            uri="s3://sandbox-artifacts/runner.tar.gz",
            sha256=digest,
        ),
    )
    assert (vm.cfg.artifacts_dir / digest / "runner" / "main.py").read_bytes() == b"print('hi')\n"
    again = await _run(
        env,
        orchestrator,
        ArtifactWorkflow,
        ArtifactParams(
            task_queue=vm.task_queue,
            uri="s3://sandbox-artifacts/runner.tar.gz",
            sha256=digest,
        ),
    )
    assert again.path == ref.path
    with pytest.raises(WorkflowFailureError):
        await _run(
            env,
            orchestrator,
            ArtifactWorkflow,
            ArtifactParams(
                task_queue=vm.task_queue,
                uri="s3://sandbox-artifacts/runner.tar.gz",
                sha256="0" * 64,
            ),
        )


async def test_describe(env, aws, vm, orchestrator):
    info = await _run(env, orchestrator, DescribeWorkflow, vm.task_queue)
    assert info.vm_id == vm.vm_id and info.running_jobs == 0 and info.contract_majors == [1]


async def test_recycling_row_makes_the_agent_wipe_and_go_idle(env, aws, vm, orchestrator):
    registry, _ = aws
    await _run(
        env,
        orchestrator,
        RunCommandWorkflow,
        RunCommandParams(
            task_queue=vm.task_queue,
            cwd=_ws(vm),
            argv=["sh", "-c", "echo x > leftover"],
        ),
    )
    assert (vm.workspace_root / "wf" / "leftover").exists()
    assert registry.set_state(vm.vm_id, "recycling", expect="idle")
    for _ in range(40):
        if registry.get_vm(vm.vm_id)["state"] == "idle":
            break
        await asyncio.sleep(0.1)
    assert registry.get_vm(vm.vm_id)["state"] == "idle"
    assert not (vm.workspace_root / "wf").exists()


async def test_drain_fails_the_running_wait_with_host_draining(env, aws, vm, orchestrator):
    registry, _ = aws
    handle = await env.client.start_workflow(
        RunCommandWorkflow.run,
        RunCommandParams(
            task_queue=vm.task_queue,
            cwd=_ws(vm),
            argv=["sleep", "30"],
            wait_start_to_close_seconds=20,
            wait_heartbeat_seconds=10,
            # One attempt, so the failure the workflow reports is the drain's
            # own and not a schedule-to-start timeout on a queue whose worker
            # has since gone away.
            wait_max_attempts=1,
        ),
        id=f"drain-{uuid.uuid4().hex[:8]}",
        task_queue=orchestrator,
    )
    await asyncio.sleep(2)
    await vm.drain()
    with pytest.raises(WorkflowFailureError) as err:
        await handle.result()
    cause = err.value.cause
    assert isinstance(cause, ActivityError)
    assert isinstance(cause.cause, ApplicationError)
    assert cause.cause.type == "HostDraining"
    assert registry.get_vm(vm.vm_id)["state"] == "terminated"


async def test_cancelling_the_workflow_kills_the_job(env, aws, vm, orchestrator):
    registry, _ = aws
    handle = await env.client.start_workflow(
        RunCommandWorkflow.run,
        RunCommandParams(
            task_queue=vm.task_queue,
            cwd=_ws(vm),
            argv=["sleep", "30"],
            wait_heartbeat_seconds=10,
        ),
        id=f"cancel-{uuid.uuid4().hex[:8]}",
        task_queue=orchestrator,
    )
    await asyncio.sleep(2)
    await handle.cancel()
    with pytest.raises(WorkflowFailureError) as err:
        await handle.result()
    assert isinstance(err.value.cause, CancelledError)

    jobs = vm.runtime.jobs
    # The activity learns of the cancellation on its next heartbeat, so the kill
    # trails the workflow's own failure.
    deadline = time.monotonic() + 30
    while jobs.running_job_ids() and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
    assert jobs.running_job_ids() == []
    job_id = registry.list_jobs(vm.vm_id)[0]["job_id"]
    assert jobs.status(job_id).reason == "cancelled"
