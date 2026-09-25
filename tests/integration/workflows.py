"""Test workflows that call VM activities by name, without the client.

They exist to prove the VM side of the contract on its own. Task 8 adds
workflows that go through the client.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from sandbox.contract import names
from sandbox.contract.types import (
    ArtifactRef,
    ArtifactRequest,
    ExecJob,
    ExecResult,
    ExecSpec,
    FileStat,
    GetFileRequest,
    PutFileRequest,
    RestoreRequest,
    SnapshotRef,
    SnapshotRequest,
    VmInfo,
    WaitRequest,
)

_ONCE = RetryPolicy(maximum_attempts=1)


@dataclass
class RunCommandParams:
    task_queue: str
    cwd: str
    argv: list[str]
    timeout_seconds: int = 30
    log_uri: str = ""
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    start_twice: bool = False
    wait_start_to_close_seconds: int = 120
    wait_heartbeat_seconds: int = 30
    wait_max_attempts: int = 3


@workflow.defn
class RunCommandWorkflow:
    @workflow.run
    async def run(self, p: RunCommandParams) -> ExecResult:
        spec = ExecSpec(
            job_id=str(workflow.uuid4()),
            argv=p.argv,
            cwd=p.cwd,
            env=p.env,
            secrets=p.secrets,
            timeout_seconds=p.timeout_seconds,
            log_uri=p.log_uri,
        )
        job = await workflow.execute_activity(
            names.EXEC_START,
            spec,
            task_queue=p.task_queue,
            start_to_close_timeout=timedelta(seconds=30),
            schedule_to_start_timeout=timedelta(seconds=10),
            retry_policy=_ONCE,
            result_type=ExecJob,
        )
        if p.start_twice:
            again = await workflow.execute_activity(
                names.EXEC_START,
                spec,
                task_queue=p.task_queue,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_ONCE,
                result_type=ExecJob,
            )
            if again != job:
                raise ApplicationError("exec_start was not idempotent", non_retryable=True)
        return await workflow.execute_activity(
            names.EXEC_WAIT,
            WaitRequest(job_id=job.job_id),
            task_queue=p.task_queue,
            schedule_to_start_timeout=timedelta(seconds=10),
            start_to_close_timeout=timedelta(seconds=p.wait_start_to_close_seconds),
            heartbeat_timeout=timedelta(seconds=p.wait_heartbeat_seconds),
            retry_policy=RetryPolicy(
                maximum_attempts=p.wait_max_attempts, initial_interval=timedelta(seconds=1)
            ),
            result_type=ExecResult,
        )


@dataclass
class FileParams:
    task_queue: str
    src_uri: str
    remote_path: str
    dst_uri: str


@workflow.defn
class FileRoundTripWorkflow:
    @workflow.run
    async def run(self, p: FileParams) -> FileStat:
        await workflow.execute_activity(
            names.PUT_FILE,
            PutFileRequest(src_uri=p.src_uri, path=p.remote_path),
            task_queue=p.task_queue,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_ONCE,
            result_type=FileStat,
        )
        return await workflow.execute_activity(
            names.GET_FILE,
            GetFileRequest(path=p.remote_path, dst_uri=p.dst_uri),
            task_queue=p.task_queue,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_ONCE,
            result_type=FileStat,
        )


@dataclass
class ArtifactParams:
    task_queue: str
    uri: str
    sha256: str


@workflow.defn
class ArtifactWorkflow:
    @workflow.run
    async def run(self, p: ArtifactParams) -> ArtifactRef:
        return await workflow.execute_activity(
            names.ENSURE_ARTIFACT,
            ArtifactRequest(uri=p.uri, sha256=p.sha256),
            task_queue=p.task_queue,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_ONCE,
            result_type=ArtifactRef,
        )


@workflow.defn
class DescribeWorkflow:
    @workflow.run
    async def run(self, task_queue: str) -> VmInfo:
        return await workflow.execute_activity(
            names.DESCRIBE,
            task_queue=task_queue,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_ONCE,
            result_type=VmInfo,
        )


@dataclass
class SnapshotParams:
    from_queue: str
    to_queue: str
    src_path: str
    dst_path: str
    uri: str


@workflow.defn
class SnapshotRestoreWorkflow:
    """Snapshot a directory on one VM and restore it on another."""

    @workflow.run
    async def run(self, p: SnapshotParams) -> SnapshotRef:
        snap = await workflow.execute_activity(
            names.SNAPSHOT,
            SnapshotRequest(path=p.src_path, dst_uri=p.uri),
            task_queue=p.from_queue,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_ONCE,
            result_type=SnapshotRef,
        )
        return await workflow.execute_activity(
            names.RESTORE,
            RestoreRequest(src_uri=snap.uri, sha256=snap.sha256, path=p.dst_path),
            task_queue=p.to_queue,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_ONCE,
            result_type=SnapshotRef,
        )
