"""Demo workflows.

What: `SmokeWorkflow`, the smallest possible use of a sandbox: lease a VM, ask
it to describe itself, run two commands, release. `HoldWorkflow` leases a VM
and sleeps, giving the chaos drills a lease that outlives a smoke run.
Why: the first thing the demo runs and the first thing to check when anything
else fails. `CodingSessionDemoWorkflow` is the demo proper: a coding session
with a fix loop that survives losing its VM.
Production: the production coding-agent workflows are the real callers.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from sandbox.client import Sandbox, Timeouts
from sandbox.contract.errors import LeaseLost
from sandbox.contract.names import WORKSPACE_ROOT
from sandbox.contract.types import ExecSpec, SandboxSpec
from sandbox.orchestrator.steps import (
    PUBLISH_SUMMARY,
    SessionSummary,
    SessionUris,
    StepContext,
    clone_spec,
    export_spec,
    lint_spec,
    run_step,
    test_spec,
    turn_spec,
)


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


@dataclass
class HoldParams:
    seconds: int = 120
    pool: str = "demo"
    profile: str = "local"
    workspace_root: str = WORKSPACE_ROOT


@dataclass
class HoldResult:
    vm_id: str
    lease_id: str
    held_seconds: int


@workflow.defn
class HoldWorkflow:
    """Lease a VM and hold it for a while.

    The chaos drills need a lease that outlives a smoke: terminate this
    workflow to create an orphan, or start several to create demand.
    """

    @workflow.run
    async def run(self, params: HoldParams) -> HoldResult:
        sandbox = Sandbox(
            Timeouts.for_profile(params.profile), workspace_root=params.workspace_root
        )
        spec = SandboxSpec(pool=params.pool, request_id=str(workflow.uuid4()))
        async with sandbox.lease(spec) as vm:
            await vm.exec(
                ExecSpec(
                    job_id=vm.new_job_id(),
                    argv=["sleep", str(params.seconds)],
                    cwd=vm.workspace(),
                    timeout_seconds=params.seconds + 60,
                )
            )
            return HoldResult(
                vm_id=vm.lease.vm_id, lease_id=vm.lease.lease_id, held_seconds=params.seconds
            )


@dataclass
class CodingSessionParams:
    session_id: str
    runner_uri: str
    runner_sha256: str
    prompt: str = "Add a multiply function to calc with a test"
    repo: str = "hello"
    scenario: str = ""  # empty: the runner picks one from the prompt
    max_turns: int = 3
    max_lease_attempts: int = 3
    turn_seconds: int = 30
    agent: str = "fake"
    pool: str = "demo"
    profile: str = "local"
    workspace_root: str = WORKSPACE_ROOT
    runner_env: dict[str, str] = field(default_factory=dict)


@dataclass
class CodingSessionResult:
    session_id: str
    turns: int
    tests_passed: bool
    tests_failed: int
    lint_findings: int
    patch_uri: str
    summary_uri: str
    vm_ids: list[str]
    attempts: int
    fake_cost_usd: float


@workflow.defn
class CodingSessionDemoWorkflow:
    """A coding session the way ProductionCodingWorkflow runs one in production.

    clone, then turn / lint / test until the tests pass or max_turns is
    reached, then export and publish a summary. Every step is one exec
    activity. Losing the VM mid-session costs the interrupted turn: the next
    lease clones the bundle the runner saved after the previous turn and the
    turn counter continues, because the counter lives in session.json, not
    here.
    """

    @workflow.run
    async def run(self, p: CodingSessionParams) -> CodingSessionResult:
        sandbox = Sandbox(Timeouts.for_profile(p.profile), workspace_root=p.workspace_root)
        uris = SessionUris(p.session_id)
        feedback_uri = ""
        vm_ids: list[str] = []
        cost = 0.0
        for attempt in range(1, p.max_lease_attempts + 1):
            spec = SandboxSpec(pool=p.pool, request_id=str(workflow.uuid4()))
            try:
                async with sandbox.lease(spec) as vm:
                    vm_ids.append(vm.lease.vm_id)
                    runner = await vm.ensure_artifact(p.runner_uri, p.runner_sha256)
                    ctx = StepContext(
                        runner_path=runner.path,
                        workspace=vm.workspace(),
                        session_id=p.session_id,
                        attempt=attempt,
                        env=p.runner_env,
                    )
                    clone = await run_step(vm, uris, clone_spec(ctx, uris, p.repo))
                    turn_no = int(clone["turn"])
                    while True:
                        turn_no += 1
                        turn = await run_step(
                            vm,
                            uris,
                            turn_spec(
                                ctx,
                                uris,
                                turn_no,
                                p.prompt,
                                p.scenario,
                                feedback_uri,
                                p.turn_seconds,
                                p.agent,
                            ),
                        )
                        cost += float(turn.get("fake_cost_usd", 0.0))
                        lint = await run_step(vm, uris, lint_spec(ctx, uris, turn_no))
                        report = await run_step(vm, uris, test_spec(ctx, uris, turn_no))
                        if report["failed"] == 0 or int(turn["turn"]) >= p.max_turns:
                            break
                        feedback_uri = uris.envelope(test_spec(ctx, uris, turn_no).job_id)
                    await run_step(vm, uris, export_spec(ctx, uris, turn_no))
                    patch = await vm.get_file(f"{ctx.workspace}/session.patch", uris.patch)
                    summary = SessionSummary(
                        session_id=p.session_id,
                        prompt=p.prompt,
                        scenario=turn.get("scenario", p.scenario),
                        turns=int(turn["turn"]),
                        tests_passed=report["failed"] == 0,
                        tests_failed=int(report["failed"]),
                        tests_total=int(report["total"]),
                        lint_count=int(lint["count"]),
                        patch_uri=patch.uri,
                        fake_cost_usd=round(cost, 4),
                        vm_ids=list(vm_ids),
                        attempts=attempt,
                    )
                    summary_uri = await workflow.execute_activity(
                        PUBLISH_SUMMARY,
                        summary,
                        start_to_close_timeout=timedelta(minutes=1),
                        retry_policy=RetryPolicy(
                            maximum_attempts=5, initial_interval=timedelta(seconds=1)
                        ),
                        result_type=str,
                    )
                    return CodingSessionResult(
                        session_id=p.session_id,
                        turns=summary.turns,
                        tests_passed=summary.tests_passed,
                        tests_failed=summary.tests_failed,
                        lint_findings=summary.lint_count,
                        patch_uri=patch.uri,
                        summary_uri=summary_uri,
                        vm_ids=list(vm_ids),
                        attempts=attempt,
                        fake_cost_usd=summary.fake_cost_usd,
                    )
            except LeaseLost as e:
                workflow.logger.warning(
                    "lease lost on attempt %d of %d: %s", attempt, p.max_lease_attempts, e.message
                )
                continue
        raise ApplicationError(
            f"sandbox lost {p.max_lease_attempts} times; giving up on {p.session_id}",
            type="SessionAbandoned",
            non_retryable=True,
        )
