"""What a session asks a VM to do, and how a step's answer comes back.

What: `ExecSpec` builders for the five runner steps, the object-store layout of
a session, the two orchestrator activity names, and `run_step`, which runs a
spec on the leased VM and reads its envelope.
Why: every step is its own exec activity with its own timeout, so the
Temporal UI shows the session's shape and a failure names the step. The runner
writes its answer to an envelope in the object store and the orchestrator reads
it through an activity, so no payload ever carries a test report. A `lost` job
is the lease being lost; a non-zero exit with a readable `ok: false` envelope
is `StepBroken`, carrying the runner's own curated error; a non-zero exit
without one is `ExecFailed` with the stderr tail. Nothing here touches the
network, because the workflow imports it.
Production: identical; the argv follows the real harness.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from sandbox.client.sandbox import Lease
from sandbox.contract.errors import ExecFailed, LeaseLost
from sandbox.contract.types import ExecSpec

READ_ENVELOPE = "orchestrator.read_envelope"
PUBLISH_SUMMARY = "orchestrator.publish_summary"
STEP_BROKEN = "StepBroken"
MAX_ENVELOPE_BYTES = 256 * 1024
STEP_TIMEOUT_SECONDS = 120
TURN_TIMEOUT_SLACK_SECONDS = 120


@dataclass(frozen=True)
class SessionUris:
    session_id: str

    @property
    def session(self) -> str:
        return f"s3://sandbox-sessions/{self.session_id}"

    def envelope(self, job_id: str) -> str:
        return f"{self.session}/steps/{job_id}.json"

    def log(self, job_id: str) -> str:
        return f"s3://sandbox-jobs/{self.session_id}/{job_id}"

    @property
    def patch(self) -> str:
        return f"s3://sandbox-out/{self.session_id}/session.patch"

    @property
    def summary(self) -> str:
        return f"s3://sandbox-out/{self.session_id}/summary.json"


@dataclass(frozen=True)
class EnvelopeRequest:
    uri: str
    kind: str


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    prompt: str
    scenario: str
    turns: int
    tests_passed: bool
    tests_failed: int
    tests_total: int
    lint_count: int
    patch_uri: str
    fake_cost_usd: float
    vm_ids: list[str]
    attempts: int


@dataclass(frozen=True)
class StepContext:
    runner_path: str
    workspace: str
    session_id: str
    attempt: int
    env: dict[str, str] = field(default_factory=dict)


def job_id(ctx: StepContext, step: str, turn: int) -> str:
    return f"{ctx.session_id}-{step}-t{turn}-a{ctx.attempt}"


def _spec(
    ctx: StepContext, uris: SessionUris, step: str, turn: int, args: list[str], timeout: int
) -> ExecSpec:
    jid = job_id(ctx, step, turn)
    return ExecSpec(
        job_id=jid,
        argv=[
            "/bin/sh",
            f"{ctx.runner_path}/bin/runner",
            step,
            "--workspace",
            ctx.workspace,
            "--envelope-uri",
            uris.envelope(jid),
            *args,
        ],
        cwd=ctx.workspace,
        env=dict(ctx.env),
        timeout_seconds=timeout,
        log_uri=uris.log(jid),
    )


def clone_spec(ctx: StepContext, uris: SessionUris, repo: str) -> ExecSpec:
    return _spec(
        ctx, uris, "clone", 0, ["--repo", repo, "--session-uri", uris.session], STEP_TIMEOUT_SECONDS
    )


def turn_spec(
    ctx: StepContext,
    uris: SessionUris,
    turn: int,
    prompt: str,
    scenario: str,
    feedback_uri: str,
    seconds: int,
    agent: str,
) -> ExecSpec:
    args = [
        "--session-uri",
        uris.session,
        "--prompt",
        prompt,
        "--scenario",
        scenario,
        "--feedback-uri",
        feedback_uri or "none",
        "--seconds",
        str(seconds),
        "--agent",
        agent,
    ]
    return _spec(ctx, uris, "turn", turn, args, seconds + TURN_TIMEOUT_SLACK_SECONDS)


def lint_spec(ctx: StepContext, uris: SessionUris, turn: int) -> ExecSpec:
    return _spec(ctx, uris, "lint", turn, [], STEP_TIMEOUT_SECONDS)


def test_spec(ctx: StepContext, uris: SessionUris, turn: int) -> ExecSpec:
    return _spec(ctx, uris, "test", turn, [], STEP_TIMEOUT_SECONDS)


def export_spec(ctx: StepContext, uris: SessionUris, turn: int) -> ExecSpec:
    return _spec(ctx, uris, "export", turn, ["--session-uri", uris.session], STEP_TIMEOUT_SECONDS)


async def _read_envelope(
    uris: SessionUris, spec: ExecSpec, kind: str, attempts: int
) -> dict | None:
    """Read a step's envelope, retrying up to `attempts` times.

    With `attempts=1` (a non-zero exit, checking for a curated error before
    falling back to `ExecFailed`) a still-unreadable envelope is expected, not
    exceptional: return None instead of letting the activity error surface.
    With more attempts (the normal, zero-exit path) a failure to ever read the
    envelope is unexpected and propagates as before.
    """
    try:
        return await workflow.execute_activity(
            READ_ENVELOPE,
            EnvelopeRequest(uri=uris.envelope(spec.job_id), kind=kind),
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(
                maximum_attempts=attempts, initial_interval=timedelta(seconds=1)
            ),
            result_type=dict,
        )
    except ActivityError:
        if attempts == 1:
            return None
        raise


async def run_step(vm: Lease, uris: SessionUris, spec: ExecSpec) -> dict:
    """Run one runner step on the VM and return its envelope.

    Outcomes are kept apart on purpose: a lost job means the VM is gone and the
    caller should lease another. A non-zero exit whose envelope is readable
    and says `ok: false` is `StepBroken`, carrying the runner's own error; a
    non-zero exit without a readable envelope is `ExecFailed` with the stderr
    tail. A zero exit still needs its envelope to say `ok: true`, or it is
    `StepBroken` too.
    """
    kind = spec.argv[2]
    result = await vm.exec(spec, check=False)
    if result.status == "lost":
        raise LeaseLost(f"{kind} job {spec.job_id} was lost with its VM")
    if result.status != "exited":
        raise ExecFailed(result)
    if result.exit_code != 0:
        envelope = await _read_envelope(uris, spec, kind, attempts=1)
        if envelope is not None and not envelope.get("ok", True):
            raise ApplicationError(
                f"{kind} step broke: {envelope.get('error', 'no error recorded')}",
                type=STEP_BROKEN,
                non_retryable=True,
            )
        raise ExecFailed(result)
    envelope = await _read_envelope(uris, spec, kind, attempts=5)
    if envelope is None or not envelope.get("ok", False):
        raise ApplicationError(
            f"{kind} step broke: {(envelope or {}).get('error', 'no error recorded')}",
            type=STEP_BROKEN,
            non_retryable=True,
        )
    return envelope
