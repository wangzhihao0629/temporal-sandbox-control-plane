"""The Lease's lost latch: once a lease is lost, no further VM call is attempted."""

import pytest

from sandbox.client import Timeouts
from sandbox.client.sandbox import Lease
from sandbox.contract.errors import LeaseLost
from sandbox.contract.types import ExecSpec, SandboxLease

SPEC = ExecSpec(job_id="j1", argv=["true"], cwd="/private/tmp/sandbox/wf")


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
