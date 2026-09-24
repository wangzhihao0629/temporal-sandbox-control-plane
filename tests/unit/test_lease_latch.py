"""The Lease's lost latch and the retry policy every VM call is made under."""

import pytest
from temporalio import workflow

from sandbox.client import Timeouts
from sandbox.client.sandbox import VM_NON_RETRYABLE, Lease
from sandbox.contract.errors import HOST_DRAINING, INCOMPATIBLE, LEASE_LOST, LeaseLost
from sandbox.contract.exec_policy import ExecPolicy, ExecRule
from sandbox.contract.types import ExecJob, ExecSpec, SandboxLease

SPEC = ExecSpec(job_id="j1", argv=["true"], cwd="/private/tmp/sandbox/wf")

# This file is about the lost latch and the retry policy, not the exec policy,
# so SPEC's argv is left simple and every argv is let through here.
_ALLOW_ALL = ExecPolicy(rules=(ExecRule("allow-all", lambda argv: True),))


def _lease() -> Lease:
    return Lease(
        SandboxLease(
            lease_id="l1",
            vm_id="sbx-1",
            task_queue="sandbox-vm-sbx-1",
            pool="demo",
            contract_version="1.0",
            expires_at="2026-01-01T00:00:00Z",
        ),
        Timeouts.test(),
        exec_policy=_ALLOW_ALL,
    )


async def test_a_lost_lease_refuses_further_calls_without_touching_temporal():
    # No workflow context here on purpose: the latch has to short-circuit before
    # any workflow.* access, or a workflow would keep hammering a dead queue.
    lease = _lease()
    lease.lost = True
    with pytest.raises(LeaseLost) as caught:
        await lease.exec_start(SPEC)
    assert "l1" in str(caught.value) and "sbx-1" in str(caught.value)


async def test_a_live_lease_gets_past_the_latch():
    # Reaching workflow.execute_activity proves the latch let the call through;
    # outside a workflow that access is what fails, not the latch.
    lease = _lease()
    assert lease.lost is False
    with pytest.raises(Exception) as caught:
        await lease.exec_start(SPEC)
    assert not isinstance(caught.value, LeaseLost)
    assert "Not in workflow event loop" in str(caught.value)


async def test_vm_calls_are_never_retried_on_the_three_terminal_types(monkeypatch):
    # HostDraining is the one that used to be missing: retrying it re-queues the
    # activity on a VM that is shutting down, so the caller waits out a
    # schedule-to-start timeout instead of being told to acquire another VM.
    assert VM_NON_RETRYABLE == (LEASE_LOST, INCOMPATIBLE, HOST_DRAINING)
    seen = {}

    async def fake_execute_activity(name, **kwargs):
        seen["name"] = name
        seen["policy"] = kwargs["retry_policy"]
        return ExecJob(job_id="j1", vm_id="sbx-1", started_at="now")

    monkeypatch.setattr(workflow, "execute_activity", fake_execute_activity)
    await _lease().exec_start(SPEC)
    assert seen["policy"].non_retryable_error_types == list(VM_NON_RETRYABLE)
    assert seen["policy"].maximum_attempts == 3
