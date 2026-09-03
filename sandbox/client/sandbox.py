"""Sandbox and Lease.

What: `Sandbox.acquire`, `Sandbox.release`, the `lease()` context manager, and
the `Lease` methods that run activities on the VM's queue.
Why: acquire loops on NoCapacity with a visible `WaitFor:SandboxCapacity`
sleep and a stable request id, so a crash never leaks a claimed VM. Every VM
call is idempotent on an id the workflow chose, translates infrastructure
failures to `LeaseLost`, and refuses further calls once the lease is lost so a
workflow cannot keep hammering a dead queue. Job outcomes are data; `exec` with
`check=True` turns a non-zero exit into `ExecFailed` for callers who want that.
`workspace_root` mirrors the agent's `SANDBOX_WORKSPACE_ROOT`: both sides default
to the contract's root, and a VM rooted elsewhere is told so at construction
rather than having every caller pass a cwd.
Production: identical, on the default root.
"""

from contextlib import asynccontextmanager
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from sandbox.client.timeouts import Timeouts
from sandbox.client.translate import translate
from sandbox.contract import names
from sandbox.contract.errors import (
    INCOMPATIBLE,
    LEASE_LOST,
    NO_CAPACITY,
    ExecFailed,
    LeaseLost,
    NoCapacity,
    SandboxUnavailable,
)
from sandbox.contract.types import (
    ArtifactRef,
    ArtifactRequest,
    CancelRequest,
    ExecJob,
    ExecResult,
    ExecSpec,
    FileStat,
    GetFileRequest,
    PutFileRequest,
    ReleaseRequest,
    SandboxLease,
    SandboxSpec,
    VmInfo,
    WaitRequest,
)


class Lease:
    def __init__(
        self,
        lease: SandboxLease,
        timeouts: Timeouts,
        workspace_root: str = names.WORKSPACE_ROOT,
    ) -> None:
        self.lease = lease
        self.t = timeouts
        self.workspace_root = workspace_root
        self.lost = False

    @staticmethod
    def new_job_id() -> str:
        return str(workflow.uuid4())

    def workspace(self, name: str | None = None) -> str:
        base = f"{self.workspace_root}/{names.sanitize_id(workflow.info().workflow_id)}"
        return f"{base}/{name}" if name else base

    async def _call(
        self,
        name: str,
        arg=None,
        *,
        start_to_close: timedelta,
        heartbeat_timeout: timedelta | None = None,
        result_type=None,
        max_attempts: int = 3,
    ):
        if self.lost:
            raise LeaseLost(f"lease {self.lease.lease_id} on {self.lease.vm_id} was lost")
        try:
            return await workflow.execute_activity(
                name,
                args=[arg] if arg is not None else [],
                task_queue=self.lease.task_queue,
                schedule_to_start_timeout=self.t.schedule_to_start,
                start_to_close_timeout=start_to_close,
                heartbeat_timeout=heartbeat_timeout,
                retry_policy=RetryPolicy(
                    maximum_attempts=max_attempts,
                    initial_interval=timedelta(seconds=1),
                    non_retryable_error_types=[LEASE_LOST, INCOMPATIBLE],
                ),
                result_type=result_type,
            )
        except ActivityError as e:
            mapped = translate(e, vm_call=True)
            if mapped is None:
                raise
            if isinstance(mapped, LeaseLost):
                self.lost = True
            raise mapped from e

    async def exec_start(self, spec: ExecSpec) -> ExecJob:
        return await self._call(
            names.EXEC_START, spec, start_to_close=self.t.short, result_type=ExecJob
        )

    async def exec_wait(self, job: ExecJob, timeout_seconds: int) -> ExecResult:
        return await self._call(
            names.EXEC_WAIT,
            WaitRequest(job_id=job.job_id),
            start_to_close=timedelta(seconds=timeout_seconds) + self.t.wait_slack,
            heartbeat_timeout=self.t.heartbeat,
            result_type=ExecResult,
        )

    async def exec(self, spec: ExecSpec, check: bool = True) -> ExecResult:
        job = await self.exec_start(spec)
        result = await self.exec_wait(job, spec.timeout_seconds)
        if check and (result.status != "exited" or result.exit_code != 0):
            raise ExecFailed(result)
        return result

    async def exec_cancel(self, job_id: str, grace_seconds: int = 10) -> None:
        await self._call(
            names.EXEC_CANCEL,
            CancelRequest(job_id=job_id, grace_seconds=grace_seconds),
            start_to_close=self.t.short,
        )

    async def put_file(self, src_uri: str, path: str) -> FileStat:
        return await self._call(
            names.PUT_FILE,
            PutFileRequest(src_uri=src_uri, path=path),
            start_to_close=self.t.file_transfer,
            heartbeat_timeout=self.t.short,
            result_type=FileStat,
        )

    async def get_file(self, path: str, dst_uri: str) -> FileStat:
        return await self._call(
            names.GET_FILE,
            GetFileRequest(path=path, dst_uri=dst_uri),
            start_to_close=self.t.file_transfer,
            heartbeat_timeout=self.t.short,
            result_type=FileStat,
        )

    async def ensure_artifact(self, uri: str, sha256: str) -> ArtifactRef:
        return await self._call(
            names.ENSURE_ARTIFACT,
            ArtifactRequest(uri=uri, sha256=sha256),
            start_to_close=self.t.file_transfer,
            heartbeat_timeout=self.t.short,
            result_type=ArtifactRef,
        )

    async def describe(self) -> VmInfo:
        return await self._call(names.DESCRIBE, start_to_close=self.t.short, result_type=VmInfo)


class Sandbox:
    def __init__(
        self,
        timeouts: Timeouts | None = None,
        workspace_root: str = names.WORKSPACE_ROOT,
    ) -> None:
        self.t = timeouts or Timeouts.local()
        self.workspace_root = workspace_root

    async def acquire(self, spec: SandboxSpec) -> Lease:
        deadline = workflow.now() + self.t.acquire_wait
        while True:
            try:
                lease = await workflow.execute_activity(
                    names.ACQUIRE,
                    spec,
                    task_queue=names.MANAGER_TASK_QUEUE,
                    start_to_close_timeout=self.t.short,
                    retry_policy=RetryPolicy(
                        maximum_attempts=3,
                        initial_interval=timedelta(seconds=1),
                        non_retryable_error_types=[NO_CAPACITY, INCOMPATIBLE],
                    ),
                    result_type=SandboxLease,
                )
                return Lease(lease, self.t, self.workspace_root)
            except ActivityError as e:
                mapped = translate(e, vm_call=False)
                if isinstance(mapped, NoCapacity):
                    if workflow.now() >= deadline:
                        raise SandboxUnavailable(
                            f"no capacity in pool {spec.pool!r} after {self.t.acquire_wait}"
                        ) from e
                    await workflow.sleep(
                        self.t.acquire_retry_delay, summary="WaitFor:SandboxCapacity"
                    )
                    continue
                if mapped is not None:
                    raise mapped from e
                raise

    async def release(self, lease: Lease, disposition: str = "recycle") -> None:
        await workflow.execute_activity(
            names.RELEASE,
            ReleaseRequest(
                vm_id=lease.lease.vm_id, lease_id=lease.lease.lease_id, disposition=disposition
            ),
            task_queue=names.MANAGER_TASK_QUEUE,
            start_to_close_timeout=self.t.short,
            retry_policy=RetryPolicy(maximum_attempts=10, initial_interval=timedelta(seconds=1)),
        )

    @asynccontextmanager
    async def lease(self, spec: SandboxSpec):
        lease = await self.acquire(spec)
        try:
            yield lease
        finally:
            await self.release(lease)
