import pytest
from sandbox.contract import errors, names
from sandbox.contract.types import ExecResult, ExecSpec, SandboxLease, SandboxSpec
from sandbox.contract.version import CONTRACT_MAJOR, CONTRACT_VERSION
from temporalio.converter import DataConverter


def test_activity_names_carry_the_major():
    assert names.EXEC_START == f"sandbox.v{CONTRACT_MAJOR}.exec_start"
    assert names.ACQUIRE == f"sandbox.v{CONTRACT_MAJOR}.acquire"
    assert names.vm_task_queue("sbx-1234abcd") == "sandbox-vm-sbx-1234abcd"
    assert CONTRACT_VERSION.startswith(f"{CONTRACT_MAJOR}.")


def test_sanitize_id_keeps_only_safe_characters():
    assert names.sanitize_id("smoke/abc:def gh") == "smoke-abc-def-gh"


@pytest.mark.parametrize(
    "value, typ",
    [
        (
            ExecSpec(
                job_id="j1",
                argv=["echo", "hi"],
                cwd="/private/tmp/sandbox/x",
                env={"A": "1"},
                secrets=["github_token"],
                timeout_seconds=5,
                log_uri="s3://sandbox-jobs/x/j1",
            ),
            ExecSpec,
        ),
        (SandboxSpec(pool="demo", request_id="r1"), SandboxSpec),
        (
            SandboxLease(
                lease_id="l1",
                vm_id="sbx-1",
                task_queue="sandbox-vm-sbx-1",
                pool="demo",
                contract_version="1.0",
                expires_at="2026-09-03T00:00:00Z",
            ),
            SandboxLease,
        ),
        (
            ExecResult(
                job_id="j1",
                status="exited",
                exit_code=0,
                stdout_tail="hi\n",
                stderr_tail="",
                log_uri="",
                duration_seconds=0.5,
            ),
            ExecResult,
        ),
    ],
)
async def test_wire_types_round_trip_through_the_default_converter(value, typ):
    payloads = await DataConverter.default.encode([value])
    [decoded] = await DataConverter.default.decode(payloads, [typ])
    assert decoded == value


def test_error_types_are_application_errors_with_stable_type_strings():
    err = errors.LeaseLost("vm gone")
    assert err.type == errors.LEASE_LOST == "LeaseLost"
    assert err.non_retryable is True
    draining = errors.HostDraining()
    assert draining.type == "HostDraining"
    assert draining.non_retryable is False
    assert draining.next_retry_delay is not None
