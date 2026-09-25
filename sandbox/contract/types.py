"""Wire dataclasses.

What: every value that crosses between a workflow and the manager or a VM.
Why: kept JSON-native (str, int, float, bool, list, dict, Optional) so
Temporal's default data converter round-trips them with no custom codec, and
frozen so nothing mutates a value after it has been sent.
Production: identical. Adding a field with a default is a minor version;
removing or renaming one is a major and gets new activity names.
"""

from dataclasses import dataclass, field

from sandbox.contract.version import CONTRACT_MAJOR

EXEC_STATUSES = ("exited", "timed_out", "cancelled", "lost")
DISPOSITIONS = ("recycle", "destroy")


@dataclass(frozen=True)
class SandboxSpec:
    pool: str
    request_id: str
    labels: dict[str, str] = field(default_factory=dict)
    hold_seconds: int = 1800
    contract_major: int = CONTRACT_MAJOR


@dataclass(frozen=True)
class SandboxLease:
    lease_id: str
    vm_id: str
    task_queue: str
    pool: str
    contract_version: str
    expires_at: str


@dataclass(frozen=True)
class ReleaseRequest:
    vm_id: str
    lease_id: str
    disposition: str = "recycle"


@dataclass(frozen=True)
class ExecSpec:
    job_id: str
    argv: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    timeout_seconds: int = 600
    log_uri: str = ""


@dataclass(frozen=True)
class ExecJob:
    job_id: str
    vm_id: str
    started_at: str


@dataclass(frozen=True)
class WaitRequest:
    job_id: str


@dataclass(frozen=True)
class CancelRequest:
    job_id: str
    grace_seconds: int = 10


@dataclass(frozen=True)
class ExecResult:
    job_id: str
    status: str
    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    log_uri: str
    duration_seconds: float


@dataclass(frozen=True)
class PutFileRequest:
    src_uri: str
    path: str


@dataclass(frozen=True)
class GetFileRequest:
    path: str
    dst_uri: str


@dataclass(frozen=True)
class FileStat:
    path: str
    size: int
    uri: str


@dataclass(frozen=True)
class ArtifactRequest:
    uri: str
    sha256: str


@dataclass(frozen=True)
class ArtifactRef:
    uri: str
    sha256: str
    path: str


@dataclass(frozen=True)
class VmInfo:
    vm_id: str
    agent_version: str
    contract_majors: list[int]
    uptime_seconds: float
    disk_free_bytes: int
    running_jobs: int


@dataclass(frozen=True)
class SnapshotRequest:
    path: str
    dst_uri: str


@dataclass(frozen=True)
class SnapshotRef:
    uri: str
    sha256: str
    size: int
    files: int
    path: str


@dataclass(frozen=True)
class RestoreRequest:
    src_uri: str
    sha256: str
    path: str
