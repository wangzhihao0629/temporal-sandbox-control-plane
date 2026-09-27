"""What the command-line entry points share.

What: a Temporal connection, a Temporal UI link for a workflow, the runner
artifact `make artifact` recorded in .env, and where a workflow's VMs come
from (pool, timeout profile, workspace root). Callers run `envfile.load()`
first, so every value below can come from .env.
Why: each entry point did this inline, so a new setting had to be added to
every one of them.
Production: whatever starts workflows in production brings its own config.
"""

import os

from temporalio.client import Client

from sandbox.contract.names import WORKSPACE_ROOT


def namespace() -> str:
    return os.environ.get("TEMPORAL_NAMESPACE", "default")


async def connect() -> Client:
    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    return await Client.connect(address, namespace=namespace())


def watch_url(workflow_id: str) -> str:
    ui = os.environ.get("TEMPORAL_UI", "http://localhost:8233")
    return f"{ui}/namespaces/{namespace()}/workflows/{workflow_id}"


def runner_artifact() -> tuple[str, str]:
    uri = os.environ.get("RUNNER_URI", "")
    sha256 = os.environ.get("RUNNER_SHA256", "")
    if not uri or not sha256:
        raise SystemExit("RUNNER_URI / RUNNER_SHA256 missing from .env: run `make artifact` first")
    return uri, sha256


def placement() -> dict[str, str]:
    """The pool, timeout profile, and workspace root a demo workflow runs with."""
    return {
        "pool": os.environ.get("SANDBOX_POOL", "demo"),
        "profile": os.environ.get("SANDBOX_PROFILE", "local"),
        "workspace_root": os.environ.get("SANDBOX_WORKSPACE_ROOT", WORKSPACE_ROOT),
    }
