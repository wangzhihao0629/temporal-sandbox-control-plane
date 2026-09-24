"""VM agent configuration.

What: everything the agent reads from its environment at boot.
Why: the provider passes identity and endpoints in at launch; the AMI or the
image bakes the paths. Keeping it in one frozen object makes the in-process
test harness a two-line construction.
Production: the same variables come from user_data on EC2. `run_as_user` is
`agent` there and in the image; tests leave it unset and run jobs as themselves.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from sandbox.contract.exec_policy import DEMO_POLICY, ExecPolicy
from sandbox.contract.names import ARTIFACTS_ROOT, WORKSPACE_ROOT


def _parse_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for part in raw.split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            labels[k.strip()] = v.strip()
    return labels


def _parse_prefetch(raw: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for part in raw.split(","):
        if "@" in part:
            uri, _, digest = part.rpartition("@")
            items.append((uri.strip(), digest.strip()))
    return items


@dataclass(frozen=True)
class AgentConfig:
    vm_id: str
    pool: str
    provider_ref: str
    temporal_address: str
    temporal_namespace: str
    agent_version: str
    jobs_dir: Path
    artifacts_dir: Path
    workspace_root: Path
    secrets_dir: Path
    run_as_user: str | None
    heartbeat_seconds: float
    graceful_shutdown_seconds: float = 30.0
    labels: dict[str, str] = field(default_factory=dict)
    prefetch: list[tuple[str, str]] = field(default_factory=list)
    exec_policy: ExecPolicy = DEMO_POLICY

    @classmethod
    def from_env(cls, env=os.environ) -> "AgentConfig":
        vm_id = env["VM_ID"]
        return cls(
            vm_id=vm_id,
            pool=env.get("POOL", "demo"),
            provider_ref=env.get("PROVIDER_REF", vm_id),
            temporal_address=env.get("TEMPORAL_ADDRESS", "localhost:7233"),
            temporal_namespace=env.get("TEMPORAL_NAMESPACE", "default"),
            agent_version=env.get("AGENT_VERSION", "dev"),
            jobs_dir=Path(env.get("SANDBOX_JOBS_DIR", "/var/lib/sandbox/jobs")),
            artifacts_dir=Path(env.get("SANDBOX_ARTIFACTS_DIR", ARTIFACTS_ROOT)),
            workspace_root=Path(env.get("SANDBOX_WORKSPACE_ROOT", WORKSPACE_ROOT)),
            secrets_dir=Path(env.get("SANDBOX_SECRETS_DIR", "/etc/sandbox/secrets")),
            run_as_user=env.get("SANDBOX_RUN_AS_USER") or None,
            heartbeat_seconds=float(env.get("SANDBOX_HEARTBEAT_SECONDS", "5")),
            graceful_shutdown_seconds=float(
                env.get("SANDBOX_GRACEFUL_SHUTDOWN_SECONDS", "30")
            ),
            labels=_parse_labels(env.get("SANDBOX_LABELS", "")),
            prefetch=_parse_prefetch(env.get("PREFETCH_ARTIFACTS", "")),
        )
