"""An in-process VM: the real AgentRuntime over temp directories.

What: a VM agent that runs in the test process, with jobs run as the current
user, no sudo, and a secrets directory holding dummy values.
Why: the reconciler's FakeProvider and the integration tests both need a VM
that boots in milliseconds and dies on command; keeping one implementation in
the package means the provider used in tests is the real `AgentRuntime`, not a
stub of it.
Production: not deployed. The VM image runs `sandbox.vm_agent.worker` instead.
"""

import uuid
from pathlib import Path

from sandbox.timeutil import now_iso
from sandbox.vm_agent.config import AgentConfig
from sandbox.vm_agent.runtime import AgentRuntime


class InProcessVm:
    def __init__(
        self,
        client,
        registry,
        store,
        tmp_path: Path,
        vm_id: str | None = None,
        pool="demo",
    ):
        self.vm_id = vm_id or f"sbx-{uuid.uuid4().hex[:8]}"
        base = tmp_path / self.vm_id
        secrets = base / "secrets"
        secrets.mkdir(parents=True, exist_ok=True)
        (secrets / "github_token").write_text("dummy-github-token\n")
        (secrets / "llm_gateway").write_text("dummy-llm-key\n")
        self.workspace_root = base / "ws"
        self.cfg = AgentConfig(
            vm_id=self.vm_id,
            pool=pool,
            provider_ref=self.vm_id,
            temporal_address="",
            temporal_namespace="default",
            agent_version="test",
            jobs_dir=base / "jobs",
            artifacts_dir=base / "artifacts",
            workspace_root=self.workspace_root,
            secrets_dir=secrets,
            run_as_user=None,
            heartbeat_seconds=0.5,
            graceful_shutdown_seconds=0.0,
        )
        self.runtime = AgentRuntime(self.cfg, client, registry, store)

    @property
    def task_queue(self) -> str:
        from sandbox.contract.names import vm_task_queue

        return vm_task_queue(self.vm_id)

    async def start(self):
        await self.runtime.start()
        self.started_at = now_iso()
        return self

    async def stop(self):
        await self.runtime.stop()

    async def restart_worker(self):
        await self.runtime.restart_worker()

    async def drain(self):
        await self.runtime.drain()
