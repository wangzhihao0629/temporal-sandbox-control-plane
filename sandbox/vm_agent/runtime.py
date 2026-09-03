"""Compose the VM agent.

What: registry registration, artifact prefetch, the Temporal worker on the
per-VM queue, the heartbeat loop, and drain, wired together with explicit
start, stop, drain, and restart_worker methods.
Why: the same object runs inside the VM image and inside the integration
tests, so the tests exercise the real boot sequence.
Production: identical.
"""

import asyncio
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from sandbox.contract.names import vm_task_queue
from sandbox.contract.version import SUPPORTED_MAJORS
from sandbox.objectstore import ObjectStore
from sandbox.registry.client import Registry
from sandbox.vm_agent.activities import VmActivities
from sandbox.vm_agent.config import AgentConfig
from sandbox.vm_agent.drain import DrainState
from sandbox.vm_agent.heartbeat import HeartbeatLoop
from sandbox.vm_agent.jobs import JobStore
from sandbox.vm_agent.wipe import wipe


class AgentRuntime:
    def __init__(self, cfg: AgentConfig, client: Client, registry: Registry, store: ObjectStore):
        self.cfg = cfg
        self.client = client
        self.registry = registry
        self.store = store
        self.drain_state = DrainState()
        self.jobs = JobStore(cfg.jobs_dir, cfg.vm_id, cfg.run_as_user)
        self.activities = VmActivities(cfg, self.jobs, registry, store, self.drain_state)
        self.stop_event = asyncio.Event()
        self.heartbeat = HeartbeatLoop(
            cfg,
            registry,
            wipe_fn=lambda: wipe(cfg, self.jobs),
            on_row_missing=self.stop_event.set,
        )
        self._worker: Worker | None = None
        self._worker_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._drain_task: asyncio.Task | None = None
        self._stopped = False

    def _new_worker(self) -> Worker:
        return Worker(
            self.client,
            task_queue=vm_task_queue(self.cfg.vm_id),
            activities=self.activities.all(),
            max_concurrent_activities=16,
            graceful_shutdown_timeout=timedelta(seconds=self.cfg.graceful_shutdown_seconds),
        )

    async def start(self) -> None:
        for path in (self.cfg.jobs_dir, self.cfg.artifacts_dir, self.cfg.workspace_root):
            path.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            self.registry.register_vm,
            self.cfg.vm_id,
            self.cfg.pool,
            self.cfg.provider_ref,
            self.cfg.agent_version,
            list(SUPPORTED_MAJORS),
            self.cfg.labels,
        )
        for uri, digest in self.cfg.prefetch:
            await asyncio.to_thread(self.activities.artifacts.ensure, uri, digest)
        self._worker = self._new_worker()
        self._worker_task = asyncio.create_task(self._worker.run())
        await asyncio.to_thread(
            self.registry.set_state, self.cfg.vm_id, "idle", expect="booting", reason="ready"
        )
        await asyncio.to_thread(
            self.registry.emit, "boot", "vm-agent", "agent ready", self.cfg.vm_id
        )
        self._heartbeat_task = asyncio.create_task(self.heartbeat.run(self.stop_event))

    async def restart_worker(self) -> None:
        """Stop the Temporal worker without touching jobs, then start a fresh one."""
        assert self._worker is not None and self._worker_task is not None
        await self._worker.shutdown()
        await self._worker_task
        self._worker = self._new_worker()
        self._worker_task = asyncio.create_task(self._worker.run())

    def request_drain(self) -> asyncio.Task:
        """Start a drain in the background, or hand back the one already running.

        A signal handler cannot await, and `drain` ends by stopping the runtime,
        which is what wakes `run_until_stopped`. Holding the task here lets that
        loop wait for the drain it caused instead of racing it to the exit.
        """
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self.drain())
        return self._drain_task

    async def drain(self) -> None:
        self.drain_state.draining = True
        await asyncio.to_thread(
            self.registry.set_state, self.cfg.vm_id, "draining", reason="shutdown requested"
        )
        await asyncio.to_thread(self.registry.emit, "drain", "vm-agent", "draining", self.cfg.vm_id)
        for job_id in self.jobs.running_job_ids():
            await asyncio.to_thread(self.jobs.cancel, job_id, 10, "cancelled")
        await self.stop(final_state="terminated")

    async def stop(self, final_state: str = "terminated") -> None:
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        if self._worker is not None:
            await self._worker.shutdown()
        if self._worker_task is not None:
            await self._worker_task
        if self._heartbeat_task is not None:
            await self._heartbeat_task
        await asyncio.to_thread(
            self.registry.set_state, self.cfg.vm_id, final_state, reason="agent stopped"
        )

    async def run_until_stopped(self) -> None:
        await self.start()
        await self.stop_event.wait()
        if self._drain_task is not None:
            await self._drain_task
        else:
            await self.stop()
