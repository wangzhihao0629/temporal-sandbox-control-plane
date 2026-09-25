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
Failure: the two phases retry separately through `Sandbox.with_lease_retries`,
each up to `max_lease_attempts` leases. Losing the build VM before the snapshot
exists starts the build over on a fresh VM, since nothing from it survived.
Losing the restore VM only repeats the restore on another VM: the snapshot is
already in the object store, which is the point of having one. Losing the build
VM after its snapshot changes nothing, because nothing calls it again.
Production: the same shape, with the real agent making the edit.
"""

import asyncio
from dataclasses import dataclass, field

from temporalio import workflow

from sandbox.client import Sandbox, Timeouts
from sandbox.client.sandbox import Lease
from sandbox.contract.names import WORKSPACE_ROOT
from sandbox.contract.types import SnapshotRef
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
    run_args: list[str] = field(default_factory=list)
    step_timeout_seconds: int = 600
    max_lease_attempts: int = 3
    pool: str = "demo"
    profile: str = "local"
    workspace_root: str = WORKSPACE_ROOT
    runner_env: dict[str, str] = field(default_factory=dict)


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
    build_attempts: int = 1
    restore_attempts: int = 1
    lost_vms: list[str] = field(default_factory=list)


@workflow.defn
class GoBuildDemoWorkflow:
    @workflow.run
    async def run(self, p: GoBuildParams) -> GoBuildResult:
        sandbox = Sandbox(Timeouts.for_profile(p.profile), workspace_root=p.workspace_root)
        uris = SessionUris(p.session_id)

        def context(vm: Lease, runner_path: str, phase: str, attempt: int) -> StepContext:
            # One directory per phase and attempt, never reused: a retry must not
            # find a half-finished clone from the attempt before it.
            return StepContext(
                runner_path=runner_path,
                workspace=vm.workspace(f"{phase}-{attempt}"),
                session_id=p.session_id,
                attempt=attempt,
                phase=phase,
                env=p.runner_env,
                step_timeout_seconds=p.step_timeout_seconds,
            )

        async def restore_and_run(
            vm: Lease, attempt: int, snap: SnapshotRef
        ) -> tuple[str, str]:
            runner, _ = await asyncio.gather(
                vm.ensure_artifact(p.runner_uri, p.runner_sha256),
                vm.restore(snap, f"{vm.workspace(f'restore-{attempt}')}/repo"),
            )
            ctx = context(vm, runner.path, "restore", attempt)
            ran = await run_step(vm, uris, run_spec(ctx, uris, p.run_args))
            return vm.lease.vm_id, ran["stdout"]

        async def build_then_restore(vm: Lease, attempt: int) -> GoBuildResult:
            runner = await vm.ensure_artifact(p.runner_uri, p.runner_sha256)
            ctx = context(vm, runner.path, "build", attempt)
            fetched = await run_step(vm, uris, fetch_spec(ctx, uris, p.repo_url, p.ref))
            await run_step(vm, uris, edit_spec(ctx, uris, p.edit))
            built = await run_step(vm, uris, build_spec(ctx, uris, p.package))
            first = await run_step(vm, uris, run_spec(ctx, uris, p.run_args))
            snap = await vm.snapshot(f"{ctx.workspace}/repo", uris.snapshot)
            # From here the snapshot exists, so losing this VM changes nothing.
            # The restore VM is leased while this lease is still held, so it is
            # another machine, not this one recycled.
            (restore_vm, restored), restore_attempt, restore_lost = (
                await sandbox.with_lease_retries(
                    p.pool,
                    lambda other, n: restore_and_run(other, n, snap),
                    p.max_lease_attempts,
                    what="the restore",
                )
            )
            return GoBuildResult(
                session_id=p.session_id,
                build_vm=vm.lease.vm_id,
                restore_vm=restore_vm,
                head=fetched["head"],
                go_version=built["go_version"],
                binary_bytes=int(built["bytes"]),
                output=first["stdout"],
                restored_output=restored,
                snapshot_uri=snap.uri,
                snapshot_bytes=snap.size,
                snapshot_files=snap.files,
                restore_attempts=restore_attempt,
                lost_vms=restore_lost,
            )

        result, build_attempt, build_lost = await sandbox.with_lease_retries(
            p.pool, build_then_restore, p.max_lease_attempts, what="the build"
        )
        result.build_attempts = build_attempt
        result.lost_vms = build_lost + result.lost_vms
        return result
