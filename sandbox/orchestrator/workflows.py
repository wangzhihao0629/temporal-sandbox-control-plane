"""Demo workflows.

What: `SmokeWorkflow`, the smallest possible use of a sandbox: lease a VM, ask
it to describe itself, run two commands, release.
Why: the first thing the demo runs and the first thing to check when anything
else fails. Plan 3 adds `CodingSessionDemoWorkflow` beside it.
Production: the production coding-agent workflows are the real callers.
"""

from dataclasses import dataclass

from temporalio import workflow

from sandbox.client import Sandbox, Timeouts
from sandbox.contract.names import WORKSPACE_ROOT
from sandbox.contract.types import ExecSpec, SandboxSpec


@dataclass
class SmokeParams:
    pool: str = "demo"
    profile: str = "local"
    workspace_root: str = WORKSPACE_ROOT


@dataclass
class SmokeResult:
    vm_id: str
    lease_id: str
    uname: str
    whoami: str
    agent_version: str


@workflow.defn
class SmokeWorkflow:
    @workflow.run
    async def run(self, params: SmokeParams) -> SmokeResult:
        sandbox = Sandbox(
            Timeouts.for_profile(params.profile), workspace_root=params.workspace_root
        )
        spec = SandboxSpec(pool=params.pool, request_id=str(workflow.uuid4()))
        async with sandbox.lease(spec) as vm:
            info = await vm.describe()
            cwd = vm.workspace()
            uname = await vm.exec(
                ExecSpec(job_id=vm.new_job_id(), argv=["uname", "-a"], cwd=cwd, timeout_seconds=30)
            )
            whoami = await vm.exec(
                ExecSpec(job_id=vm.new_job_id(), argv=["id"], cwd=cwd, timeout_seconds=30)
            )
            return SmokeResult(
                vm_id=info.vm_id,
                lease_id=vm.lease.lease_id,
                uname=uname.stdout_tail.strip(),
                whoami=whoami.stdout_tail.strip(),
                agent_version=info.agent_version,
            )
