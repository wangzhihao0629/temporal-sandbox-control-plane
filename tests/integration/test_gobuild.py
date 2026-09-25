"""GoBuildDemoWorkflow end to end on in-process VMs: the happy path, losing the build
VM mid-build, losing the restore VM, and giving up once the lease attempts run out.

These clone github.com/golang/example and build it with the host's Go, so they need
network access and a Go toolchain and are skipped without either. The fetch step
accepts https only, on purpose, so there is no offline stand-in for the clone.
"""

import asyncio
import shutil
import socket
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.worker import Worker

from sandbox.contract.names import MANAGER_TASK_QUEUE, ORCHESTRATOR_TASK_QUEUE
from sandbox.manager.activities import ManagerActivities
from sandbox.orchestrator.activities import OrchestratorActivities
from sandbox.orchestrator.gobuild_workflow import GAVE_UP, GoBuildDemoWorkflow, GoBuildParams
from sandbox.testing.inprocess_vm import InProcessVm
from sandbox.testing.stubs import StubProvider


def _github_reachable(attempts: int = 3) -> bool:
    # One dropped probe must not silently skip the whole file, so try a few times.
    for _ in range(attempts):
        try:
            socket.create_connection(("github.com", 443), timeout=5).close()
            return True
        except OSError:
            continue
    return False


pytestmark = pytest.mark.skipif(
    shutil.which("go") is None or not _github_reachable(),
    reason="needs a Go toolchain and network access to github.com",
)


@pytest.fixture
async def workers(env, aws_server):
    registry, store = aws_server
    async with (
        Worker(
            env.client,
            task_queue=MANAGER_TASK_QUEUE,
            activities=ManagerActivities(registry, StubProvider()).all(),
            activity_executor=ThreadPoolExecutor(4),
        ),
        Worker(
            env.client,
            task_queue=ORCHESTRATOR_TASK_QUEUE,
            workflows=[GoBuildDemoWorkflow],
            activities=OrchestratorActivities(store).all(),
        ),
    ):
        yield


@pytest.fixture
def pool():
    # moto keeps its tables for the whole test process, so rows from earlier tests
    # would otherwise compete for leases. A pool of its own isolates each test.
    return f"go-{uuid.uuid4().hex[:6]}"


async def _start_vms(env, aws_server, tmp_path, pool, names):
    """Start VMs one after another, so `acquire` (oldest idle first) takes them in order.
    Returns them keyed by the short name; each VM id is prefixed with the pool."""
    registry, store = aws_server
    vms = {}
    for name in names:
        vms[name] = await InProcessVm(
            env.client,
            registry,
            store,
            tmp_path,
            vm_id=f"{pool}-{name}",
            pool=pool,
            workspace_root=tmp_path / "ws",
        ).start()
        await asyncio.sleep(0.05)
    return vms


async def _stop(vms, crashed):
    for name, vm in vms.items():
        if name not in crashed:
            await vm.stop()


def _params(runner_artifact, tmp_path, pool, **overrides) -> GoBuildParams:
    uri, sha = runner_artifact
    return GoBuildParams(
        session_id=f"{pool}-s",
        pool=pool,
        runner_uri=uri,
        runner_sha256=sha,
        profile="test",
        workspace_root=str(tmp_path / "ws"),
        runner_env={"RUNNER_PYTHON": sys.executable},
        **overrides,
    )


def _start(env, params):
    return env.client.start_workflow(
        GoBuildDemoWorkflow.run,
        params,
        id=f"gobuild-{params.session_id}",
        task_queue=ORCHESTRATOR_TASK_QUEUE,
    )


async def _wait_for_job(registry, vm_id, job_id, seconds=60):
    for _ in range(int(seconds / 0.1)):
        if any(row["job_id"] == job_id for row in registry.list_jobs(vm_id)):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{job_id} never started on {vm_id}")


async def test_clone_edit_build_run_then_restore_on_another_vm(
    env, aws_server, workers, runner_artifact, tmp_path, pool
):
    vms = await _start_vms(env, aws_server, tmp_path, pool, ["a", "b"])
    try:
        handle = await _start(env, _params(runner_artifact, tmp_path, pool))
        result = await asyncio.wait_for(handle.result(), timeout=240)
        assert result.output == "Hello, Temporal sandbox!"
        assert result.restored_output == result.output
        assert (result.build_vm, result.restore_vm) == (vms["a"].vm_id, vms["b"].vm_id)
        assert result.build_attempts == 1 and result.restore_attempts == 1
        assert result.lost_vms == []
    finally:
        await _stop(vms, crashed=())


async def test_losing_the_build_vm_before_the_snapshot_rebuilds_on_a_fresh_vm(
    env, aws_server, workers, runner_artifact, tmp_path, pool
):
    registry, _ = aws_server
    vms = await _start_vms(env, aws_server, tmp_path, pool, ["a", "b", "c"])
    try:
        params = _params(runner_artifact, tmp_path, pool)
        handle = await _start(env, params)
        # Kill the build VM as soon as its first step exists: well before the snapshot.
        await _wait_for_job(registry, vms["a"].vm_id, f"{params.session_id}-fetch-t0-a1")
        await vms["a"].runtime.crash()

        result = await asyncio.wait_for(handle.result(), timeout=240)
        assert result.lost_vms == [vms["a"].vm_id]
        assert result.build_attempts == 2 and result.build_vm == vms["b"].vm_id
        assert result.restore_attempts == 1 and result.restore_vm == vms["c"].vm_id
        assert result.restored_output == result.output == "Hello, Temporal sandbox!"
    finally:
        await _stop(vms, crashed=("a",))


async def test_losing_the_restore_vm_restores_elsewhere_without_rebuilding(
    env, aws_server, workers, runner_artifact, tmp_path, pool
):
    registry, _ = aws_server
    vms = await _start_vms(env, aws_server, tmp_path, pool, ["a", "b", "c"])
    # b dies while idle. With no reconciler running its row stays `idle`, so it is
    # the VM the restore phase acquires first, and finds nobody home.
    await vms["b"].runtime.crash()
    try:
        handle = await _start(env, _params(runner_artifact, tmp_path, pool))
        result = await asyncio.wait_for(handle.result(), timeout=240)
        assert result.lost_vms == [vms["b"].vm_id]
        assert result.build_attempts == 1 and result.build_vm == vms["a"].vm_id
        assert result.restore_attempts == 2 and result.restore_vm == vms["c"].vm_id
        assert result.restored_output == result.output
        jobs = [r["job_id"] for vm in vms.values() for r in registry.list_jobs(vm.vm_id)]
        assert sum("-build-" in j for j in jobs) == 1, "the restore retry must not rebuild"
    finally:
        await _stop(vms, crashed=("b",))


async def test_running_out_of_restore_attempts_fails_with_a_clear_error(
    env, aws_server, workers, runner_artifact, tmp_path, pool
):
    vms = await _start_vms(env, aws_server, tmp_path, pool, ["a", "b", "c"])
    await vms["b"].runtime.crash()
    await vms["c"].runtime.crash()
    try:
        params = _params(runner_artifact, tmp_path, pool, max_lease_attempts=2)
        handle = await _start(env, params)
        with pytest.raises(WorkflowFailureError) as err:
            await asyncio.wait_for(handle.result(), timeout=240)
        cause = err.value.cause
        assert getattr(cause, "type", None) == GAVE_UP
        assert "restore" in str(cause)
        assert vms["b"].vm_id in str(cause) and vms["c"].vm_id in str(cause)
    finally:
        await _stop(vms, crashed=("b", "c"))
