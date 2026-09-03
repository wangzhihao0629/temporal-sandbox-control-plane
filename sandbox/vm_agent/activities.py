"""The VM agent's activities: the VM side of the contract.

What: exec_start, exec_wait, exec_cancel, put_file, get_file, ensure_artifact,
describe, registered under the versioned names in sandbox.contract.names.
Why: these are the only things a workflow can ask a VM to do. Every one is
idempotent on its key, validates its input, touches the lease so it does not
expire under a running job, and never returns bytes larger than a tail.
Production: identical.
"""

import asyncio
import os
import shutil
import subprocess
import time

from temporalio import activity

from sandbox.contract import names
from sandbox.contract.errors import HostDraining
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
    VmInfo,
    WaitRequest,
)
from sandbox.contract.version import SUPPORTED_MAJORS
from sandbox.objectstore import ObjectStore
from sandbox.registry.client import Registry
from sandbox.timeutil import now_iso
from sandbox.vm_agent import files
from sandbox.vm_agent.artifacts import ArtifactCache
from sandbox.vm_agent.config import AgentConfig
from sandbox.vm_agent.drain import DrainState
from sandbox.vm_agent.jobs import JobStore
from sandbox.vm_agent.validation import (
    resolve_secrets,
    validate_cwd,
    validate_env,
    validate_path_under,
)

_HEARTBEAT_EVERY = 5.0
_POLL_EVERY = 1.0
_VM_IDENTITY_KEYS = (
    "S3_ENDPOINT",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_DEFAULT_REGION",
)


class VmActivities:
    def __init__(
        self,
        cfg: AgentConfig,
        jobs: JobStore,
        registry: Registry,
        store: ObjectStore,
        drain: DrainState,
    ) -> None:
        self.cfg = cfg
        self.jobs = jobs
        self.registry = registry
        self.store = store
        self.drain = drain
        self.artifacts = ArtifactCache(cfg.artifacts_dir, store)
        self.started_at = time.time()

    def all(self) -> list:
        return [
            self.exec_start,
            self.exec_wait,
            self.exec_cancel,
            self.put_file,
            self.get_file,
            self.ensure_artifact,
            self.describe,
        ]

    # ---- helpers -------------------------------------------------------------

    def _job_env(self, spec: ExecSpec) -> dict[str, str]:
        if self.cfg.run_as_user:
            env = {
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "HOME": f"/home/{self.cfg.run_as_user}",
            }
        else:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(self.cfg.workspace_root),
            }
        env.update(
            {
                "LANG": "C.UTF-8",
                "SANDBOX_VM_ID": self.cfg.vm_id,
                "SANDBOX_JOB_ID": spec.job_id,
                "SANDBOX_WORKSPACE_ROOT": str(self.cfg.workspace_root),
            }
        )
        env.update(validate_env(spec.env))
        # The VM's own object-store identity, never the caller's, and applied
        # after the caller's env so a request cannot redirect the VM at another
        # endpoint. In production this is the instance's narrow role; locally it
        # is the MinIO user.
        for key in _VM_IDENTITY_KEYS:
            if key in os.environ:
                env[key] = os.environ[key]
        env.update(resolve_secrets(spec.secrets, self.cfg.secrets_dir))
        return env

    async def _touch(self) -> None:
        """Keep the lease alive; a no-op on a VM nobody has leased."""
        await asyncio.to_thread(self.registry.touch_lease, self.cfg.vm_id)

    def _mkdir_cwd(self, cwd) -> None:
        if self.cfg.run_as_user:
            subprocess.run(
                ["sudo", "-n", "-u", self.cfg.run_as_user, "--", "mkdir", "-p", str(cwd)],
                check=True,
            )
        else:
            os.makedirs(cwd, exist_ok=True)

    def _sync_logs(self, job_id: str, log_uri: str) -> dict[str, int]:
        sizes = {}
        for stream in ("stdout", "stderr"):
            path = self.jobs.log_path(job_id, stream)
            sizes[stream] = path.stat().st_size if path.exists() else 0
            if log_uri and path.exists():
                self.store.upload_file(path, f"{log_uri.rstrip('/')}/{stream}.log")
        return sizes

    def _result(self, job_id: str, spec: ExecSpec) -> ExecResult:
        st = self.jobs.status(job_id)
        if st.lost:
            status = "lost"
        elif st.reason in ("timed_out", "cancelled"):
            status = st.reason
        else:
            status = "exited"
        ended = st.ended_at or time.time()
        return ExecResult(
            job_id=job_id,
            status=status,
            exit_code=st.exit_code,
            stdout_tail=self.jobs.read_tail(job_id, "stdout"),
            stderr_tail=self.jobs.read_tail(job_id, "stderr"),
            log_uri=spec.log_uri,
            duration_seconds=max(0.0, ended - st.started_at),
        )

    # ---- activities ----------------------------------------------------------

    @activity.defn(name=names.EXEC_START)
    async def exec_start(self, spec: ExecSpec) -> ExecJob:
        cwd = validate_cwd(spec.cwd, self.cfg.workspace_root)
        env = self._job_env(spec)
        if not self.jobs.exists(spec.job_id):
            await asyncio.to_thread(self._mkdir_cwd, cwd)
        job = await asyncio.to_thread(self.jobs.start, spec, env)
        info = activity.info()
        await asyncio.to_thread(
            self.registry.put_job,
            self.cfg.vm_id,
            spec.job_id,
            status="running",
            argv_summary=" ".join(spec.argv)[:200],
            owner_workflow_id=info.workflow_id,
            started_at=job.started_at,
            log_uri=spec.log_uri,
        )
        await self._touch()
        return job

    @activity.defn(name=names.EXEC_WAIT)
    async def exec_wait(self, req: WaitRequest) -> ExecResult:
        spec = await asyncio.to_thread(self.jobs.load_spec, req.job_id)
        last_heartbeat = 0.0
        try:
            while True:
                # Draining is checked before the job's own state: the drain kills
                # the job itself, so a wait that looked at liveness first would
                # report the drain's kill as an ordinary ending and let the
                # orchestrator believe the command ran to completion here.
                if self.drain.draining:
                    await asyncio.to_thread(self.jobs.cancel, req.job_id, 10, "cancelled")
                    await asyncio.to_thread(self._sync_logs, req.job_id, spec.log_uri)
                    raise HostDraining()
                st = self.jobs.status(req.job_id)
                if not st.running:
                    break
                if time.time() - st.started_at > spec.timeout_seconds:
                    await asyncio.to_thread(self.jobs.cancel, req.job_id, 5, "timed_out")
                    break
                now = time.time()
                if now - last_heartbeat >= _HEARTBEAT_EVERY:
                    sizes = await asyncio.to_thread(self._sync_logs, req.job_id, spec.log_uri)
                    activity.heartbeat(
                        {
                            "job_id": req.job_id,
                            "elapsed": round(now - st.started_at),
                            **sizes,
                        }
                    )
                    await self._touch()
                    last_heartbeat = now
                await asyncio.sleep(_POLL_EVERY)
        except asyncio.CancelledError:
            if self.drain.draining:
                # A drain stops the worker, so the cancellation usually arrives
                # before the loop's own drain check fires. The orchestrator has
                # to hear that the host is going away — that it should re-dispatch
                # elsewhere — and not that its job was cancelled or that the
                # worker will reattach to it.
                await asyncio.to_thread(self.jobs.cancel, req.job_id, 10, "cancelled")
                await asyncio.to_thread(self._sync_logs, req.job_id, spec.log_uri)
                raise HostDraining() from None
            if activity.is_worker_shutdown():
                # Leave the job running; the retried wait will reattach to it.
                raise
            await asyncio.to_thread(self.jobs.cancel, req.job_id, 10, "cancelled")
            await asyncio.to_thread(self._sync_logs, req.job_id, spec.log_uri)
            raise
        await asyncio.to_thread(self._sync_logs, req.job_id, spec.log_uri)
        result = self._result(req.job_id, spec)
        await asyncio.to_thread(
            self.registry.update_job,
            self.cfg.vm_id,
            req.job_id,
            status=result.status,
            exit_code=result.exit_code,
            ended_at=now_iso(),
            duration_seconds=round(result.duration_seconds, 3),
        )
        return result

    @activity.defn(name=names.EXEC_CANCEL)
    async def exec_cancel(self, req: CancelRequest) -> None:
        await self._touch()
        await asyncio.to_thread(self.jobs.cancel, req.job_id, req.grace_seconds, "cancelled")

    @activity.defn(name=names.PUT_FILE)
    async def put_file(self, req: PutFileRequest) -> FileStat:
        path = validate_path_under(req.path, self.cfg.workspace_root)
        await self._touch()
        return await asyncio.to_thread(files.put_file, self.store, req.src_uri, path)

    @activity.defn(name=names.GET_FILE)
    async def get_file(self, req: GetFileRequest) -> FileStat:
        path = validate_path_under(req.path, self.cfg.workspace_root)
        await self._touch()
        return await asyncio.to_thread(files.get_file, self.store, path, req.dst_uri)

    @activity.defn(name=names.ENSURE_ARTIFACT)
    async def ensure_artifact(self, req: ArtifactRequest) -> ArtifactRef:
        await self._touch()
        path = await asyncio.to_thread(self.artifacts.ensure, req.uri, req.sha256)
        return ArtifactRef(uri=req.uri, sha256=req.sha256, path=str(path))

    @activity.defn(name=names.DESCRIBE)
    async def describe(self) -> VmInfo:
        await self._touch()
        usage = shutil.disk_usage(self.cfg.workspace_root)
        return VmInfo(
            vm_id=self.cfg.vm_id,
            agent_version=self.cfg.agent_version,
            contract_majors=list(SUPPORTED_MAJORS),
            uptime_seconds=round(time.time() - self.started_at, 1),
            disk_free_bytes=usage.free,
            running_jobs=len(self.jobs.running_job_ids()),
        )
