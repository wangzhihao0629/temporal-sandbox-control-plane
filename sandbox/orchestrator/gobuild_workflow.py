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
Failure: the two phases retry separately, each up to `max_lease_attempts`
leases. Losing the build VM before the snapshot exists starts the build over on
a fresh VM, since nothing from it survived. Losing the restore VM only repeats
the restore on another VM: the snapshot is already in the object store, which
is the point of having one. Losing the build VM after its snapshot changes
nothing, because nothing calls it again.
Production: the same shape, with the real agent making the edit.
"""

from dataclasses import dataclass, field

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from sandbox.client import Sandbox, Timeouts
from sandbox.client.sandbox import Lease
from sandbox.contract.errors import LeaseLost
from sandbox.contract.names import WORKSPACE_ROOT
from sandbox.contract.types import SandboxSpec, SnapshotRef
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
GAVE_UP = "LeaseAttemptsExhausted"


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
        self.p = p
        self.sandbox = Sandbox(Timeouts.for_profile(p.profile), workspace_root=p.workspace_root)
        self.uris = SessionUris(p.session_id)
        self.args = list(p.run_args or [])
        self.lost: list[str] = []

        for attempt in range(1, p.max_lease_attempts + 1):
            vm_id = ""
            try:
                async with self.sandbox.lease(self._request()) as vm:
                    vm_id = vm.lease.vm_id
                    built = await self._build(vm, attempt)
                    # The snapshot exists from here on, so no loss after this line
                    # sends the workflow back to the build.
                    restored = await self._restore_elsewhere(built["snapshot"])
                    return self._result(vm_id, attempt, built, restored)
            except LeaseLost:
                self.lost.append(vm_id)
                workflow.logger.warning(f"build VM {vm_id} lost on attempt {attempt}; rebuilding")
        raise ApplicationError(
            f"gave up after losing {p.max_lease_attempts} build VMs: {self.lost}",
            type=GAVE_UP,
            non_retryable=True,
        )

    def _request(self) -> SandboxSpec:
        return SandboxSpec(pool=self.p.pool, request_id=str(workflow.uuid4()))

    def _context(self, runner_path: str, workspace: str, attempt: int) -> StepContext:
        return StepContext(
            runner_path=runner_path,
            workspace=workspace,
            session_id=self.p.session_id,
            attempt=attempt,
            env=self.p.runner_env,
            step_timeout_seconds=self.p.step_timeout_seconds,
        )

    async def _build(self, vm: Lease, attempt: int) -> dict:
        p, uris = self.p, self.uris
        runner = await vm.ensure_artifact(p.runner_uri, p.runner_sha256)
        # One directory per phase and attempt, never reused: a retry must not find
        # a half-finished clone from the attempt before it.
        ctx = self._context(runner.path, vm.workspace(f"build-{attempt}"), attempt)
        fetched = await run_step(vm, uris, fetch_spec(ctx, uris, p.repo_url, p.ref))
        await run_step(vm, uris, edit_spec(ctx, uris, p.edit))
        built = await run_step(vm, uris, build_spec(ctx, uris, p.package))
        first = await run_step(vm, uris, run_spec(ctx, uris, self.args))
        snap = await vm.snapshot(f"{ctx.workspace}/repo", uris.snapshot)
        return {"fetched": fetched, "built": built, "first": first, "snapshot": snap}

    async def _restore_elsewhere(self, snap: SnapshotRef) -> dict:
        p, uris = self.p, self.uris
        for attempt in range(1, p.max_lease_attempts + 1):
            vm_id = ""
            try:
                # Acquired while the build lease is still held, so it is another VM.
                async with self.sandbox.lease(self._request()) as other:
                    vm_id = other.lease.vm_id
                    runner = await other.ensure_artifact(p.runner_uri, p.runner_sha256)
                    # Job ids carry the attempt; restore attempts are numbered after
                    # every possible build attempt so the two can never collide.
                    ctx = self._context(
                        runner.path,
                        other.workspace(f"restore-{attempt}"),
                        p.max_lease_attempts + attempt,
                    )
                    await other.restore(snap, f"{ctx.workspace}/repo")
                    second = await run_step(other, uris, run_spec(ctx, uris, self.args))
                    return {"vm_id": vm_id, "attempt": attempt, "second": second}
            except LeaseLost:
                self.lost.append(vm_id)
                workflow.logger.warning(f"restore VM {vm_id} lost on attempt {attempt}; retrying")
        # Not LeaseLost: the build phase must not read this as its own VM dying.
        raise ApplicationError(
            f"gave up after losing {p.max_lease_attempts} restore VMs: {self.lost}",
            type=GAVE_UP,
            non_retryable=True,
        )

    def _result(self, build_vm: str, attempt: int, built: dict, restored: dict) -> GoBuildResult:
        snap: SnapshotRef = built["snapshot"]
        return GoBuildResult(
            session_id=self.p.session_id,
            build_vm=build_vm,
            restore_vm=restored["vm_id"],
            head=built["fetched"]["head"],
            go_version=built["built"]["go_version"],
            binary_bytes=int(built["built"]["bytes"]),
            output=built["first"]["stdout"],
            restored_output=restored["second"]["stdout"],
            snapshot_uri=snap.uri,
            snapshot_bytes=snap.size,
            snapshot_files=snap.files,
            build_attempts=attempt,
            restore_attempts=restored["attempt"],
            lost_vms=list(self.lost),
        )
