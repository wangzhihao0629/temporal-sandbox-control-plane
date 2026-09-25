"""The wire contract: activity names, round-tripping types, and error type strings."""

import dataclasses

import pytest
from temporalio.converter import DataConverter

from sandbox.contract import errors, names, types
from sandbox.contract.types import (
    ArtifactRef,
    ArtifactRequest,
    CancelRequest,
    ExecJob,
    ExecResult,
    ExecSpec,
    FileStat,
    GetFileRequest,
    PutFileRequest,
    ReleaseRequest,
    RestoreRequest,
    SandboxLease,
    SandboxSpec,
    SnapshotRef,
    SnapshotRequest,
    VmInfo,
    WaitRequest,
)
from sandbox.contract.version import CONTRACT_MAJOR, CONTRACT_VERSION

# One populated instance of every dataclass in sandbox.contract.types. Defaults
# are filled in on purpose: a field that only ever travels as its default is a
# field whose serialization nobody has checked.
WIRE_VALUES = [
    SandboxSpec(
        pool="demo",
        request_id="r1",
        labels={"arch": "arm64"},
        hold_seconds=60,
        contract_major=CONTRACT_MAJOR,
    ),
    SandboxLease(
        lease_id="l1",
        vm_id="sbx-1",
        task_queue="sandbox-vm-sbx-1",
        pool="demo",
        contract_version=CONTRACT_VERSION,
        expires_at="2026-09-03T00:00:00Z",
    ),
    ReleaseRequest(vm_id="sbx-1", lease_id="l1", disposition="destroy"),
    ExecSpec(
        job_id="j1",
        argv=["echo", "hi"],
        cwd="/private/tmp/sandbox/x",
        env={"A": "1"},
        secrets=["github_token"],
        timeout_seconds=5,
        log_uri="s3://sandbox-jobs/x/j1",
    ),
    ExecJob(job_id="j1", vm_id="sbx-1", started_at="2026-09-03T00:00:00Z"),
    WaitRequest(job_id="j1"),
    CancelRequest(job_id="j1", grace_seconds=3),
    ExecResult(
        job_id="j1",
        status="exited",
        exit_code=0,
        stdout_tail="hi\n",
        stderr_tail="",
        log_uri="s3://sandbox-jobs/x/j1",
        duration_seconds=0.5,
    ),
    PutFileRequest(src_uri="s3://sandbox-out/in.txt", path="/private/tmp/sandbox/x/in.txt"),
    GetFileRequest(path="/private/tmp/sandbox/x/out.txt", dst_uri="s3://sandbox-out/out.txt"),
    FileStat(path="/private/tmp/sandbox/x/in.txt", size=7, uri="s3://sandbox-out/in.txt"),
    ArtifactRequest(uri="s3://sandbox-artifacts/runner.tar.gz", sha256="0" * 64),
    ArtifactRef(uri="s3://sandbox-artifacts/runner.tar.gz", sha256="0" * 64, path="/var/x"),
    VmInfo(
        vm_id="sbx-1",
        agent_version="test",
        contract_majors=[CONTRACT_MAJOR],
        uptime_seconds=1.5,
        disk_free_bytes=1024,
        running_jobs=2,
    ),
    SnapshotRequest(path="/private/tmp/sandbox/x/repo", dst_uri="s3://sandbox-sessions/x/s.tgz"),
    SnapshotRef(
        uri="s3://sandbox-sessions/x/s.tgz",
        sha256="0" * 64,
        size=1024,
        files=3,
        path="/private/tmp/sandbox/x/repo",
    ),
    RestoreRequest(
        src_uri="s3://sandbox-sessions/x/s.tgz",
        sha256="0" * 64,
        path="/private/tmp/sandbox/x/repo",
    ),
]


def test_activity_names_carry_the_major():
    assert names.EXEC_START == f"sandbox.v{CONTRACT_MAJOR}.exec_start"
    assert names.ACQUIRE == f"sandbox.v{CONTRACT_MAJOR}.acquire"
    assert names.SNAPSHOT in names.VM_OPERATIONS and names.RESTORE in names.VM_OPERATIONS
    assert names.vm_task_queue("sbx-1234abcd") == "sandbox-vm-sbx-1234abcd"
    assert CONTRACT_VERSION.startswith(f"{CONTRACT_MAJOR}.")


def test_sanitize_id_keeps_only_safe_characters():
    assert names.sanitize_id("smoke/abc:def gh") == "smoke-abc-def-gh"


def test_every_wire_dataclass_has_a_round_trip_case():
    # The parametrized test below is only worth as much as this list, so the
    # list is checked against the module rather than kept in sync by hand.
    declared = {
        value
        for value in vars(types).values()
        if dataclasses.is_dataclass(value) and value.__module__ == types.__name__
    }
    assert {type(v) for v in WIRE_VALUES} == declared


@pytest.mark.parametrize("value", WIRE_VALUES, ids=lambda v: type(v).__name__)
async def test_wire_types_round_trip_through_the_default_converter(value):
    payloads = await DataConverter.default.encode([value])
    [decoded] = await DataConverter.default.decode(payloads, [type(value)])
    assert decoded == value


def test_error_types_are_application_errors_with_stable_type_strings():
    err = errors.LeaseLost("vm gone")
    assert err.type == errors.LEASE_LOST == "LeaseLost"
    assert err.non_retryable is True
    draining = errors.HostDraining()
    assert draining.type == "HostDraining"
    assert draining.non_retryable is False
    assert draining.next_retry_delay is not None
