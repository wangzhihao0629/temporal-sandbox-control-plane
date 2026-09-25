"""The Go build demo: clone, edit, build, run, snapshot, restore elsewhere.

What: `GoBuildDemoWorkflow` leases a VM, clones a Go repository over HTTPS at a
pinned ref, applies one scripted edit, builds and runs it, and snapshots the
repo directory. Still holding that lease, it leases a second VM, restores the
snapshot there, and runs the restored binary without rebuilding.
Why: it exercises what the coding session does not — a real repository from
the network, a compiled toolchain, and a snapshot taken on one machine and
restored on another. Holding the first lease while acquiring the second makes
the second VM a different machine, not the first one recycled. The restored
run matching the first run's output is the proof the snapshot carried both the
edit and the build output.
Production: the same shape, with the real agent making the edit.
"""

from dataclasses import dataclass

from temporalio import workflow

from sandbox.client import Sandbox, Timeouts
from sandbox.contract.names import WORKSPACE_ROOT
from sandbox.contract.types import SandboxSpec
from sandbox.orchestrator.steps import (
    SessionUris,
    StepContext,
    build_spec,
    edit_spec,
    fetch_spec,
    run_spec,
    run_step,
)

DEFAULT_REPO_URL = "https://github.com/golang/example"
# Pinned so the scripted edit always finds its target text.
DEFAULT_REF = "7f05d217867b2af52b0a28c6d1c91df97e1b5b39"


@dataclass
class GoBuildParams:
    session_id: str
    runner_uri: str
    runner_sha256: str
    repo_url: str = DEFAULT_REPO_URL
    ref: str = DEFAULT_REF
    edit: str = "greet-sandbox"
    package: str = "hello"
    run_args: list[str] | None = None
    step_timeout_seconds: int = 600
    pool: str = "demo"
    profile: str = "local"
    workspace_root: str = WORKSPACE_ROOT


@dataclass
class GoBuildResult:
    session_id: str
    build_vm: str
    restore_vm: str
    head: str
    go_version: str
    binary_bytes: int
    output: str
    restored_output: str
    snapshot_uri: str
    snapshot_bytes: int
    snapshot_files: int


@workflow.defn
class GoBuildDemoWorkflow:
    @workflow.run
    async def run(self, p: GoBuildParams) -> GoBuildResult:
        sandbox = Sandbox(Timeouts.for_profile(p.profile), workspace_root=p.workspace_root)
        uris = SessionUris(p.session_id)
        args = list(p.run_args or [])

        def context(runner_path: str, workspace: str, attempt: int) -> StepContext:
            return StepContext(
                runner_path=runner_path,
                workspace=workspace,
                session_id=p.session_id,
                attempt=attempt,
                step_timeout_seconds=p.step_timeout_seconds,
            )

        request = SandboxSpec(pool=p.pool, request_id=str(workflow.uuid4()))
        async with sandbox.lease(request) as vm:
            runner = await vm.ensure_artifact(p.runner_uri, p.runner_sha256)
            ctx = context(runner.path, vm.workspace(), attempt=1)
            fetched = await run_step(vm, uris, fetch_spec(ctx, uris, p.repo_url, p.ref))
            await run_step(vm, uris, edit_spec(ctx, uris, p.edit))
            built = await run_step(vm, uris, build_spec(ctx, uris, p.package))
            first = await run_step(vm, uris, run_spec(ctx, uris, args))
            snap = await vm.snapshot(f"{ctx.workspace}/repo", uris.snapshot)

            # Acquired while the first lease is still held, so it is another VM.
            request = SandboxSpec(pool=p.pool, request_id=str(workflow.uuid4()))
            async with sandbox.lease(request) as other:
                runner2 = await other.ensure_artifact(p.runner_uri, p.runner_sha256)
                ctx2 = context(runner2.path, other.workspace(), attempt=2)
                await other.restore(snap, f"{ctx2.workspace}/repo")
                second = await run_step(other, uris, run_spec(ctx2, uris, args))
                return GoBuildResult(
                    session_id=p.session_id,
                    build_vm=vm.lease.vm_id,
                    restore_vm=other.lease.vm_id,
                    head=fetched["head"],
                    go_version=built["go_version"],
                    binary_bytes=int(built["bytes"]),
                    output=first["stdout"],
                    restored_output=second["stdout"],
                    snapshot_uri=snap.uri,
                    snapshot_bytes=snap.size,
                    snapshot_files=snap.files,
                )
