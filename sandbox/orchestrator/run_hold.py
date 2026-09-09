"""Start a HoldWorkflow and return immediately.

What: start `HoldWorkflow`, which leases a VM and sleeps, and print the
workflow id and a Temporal UI link.
Why: a chaos drill needs a lease that outlives a smoke run — `make terminate
WF=<id>` on the printed id turns it into an orphan for the reconciler to
reclaim.
Production: a CLI or UI action that starts a workflow.

Usage: uv run python -m sandbox.orchestrator.run_hold --seconds 120
"""

import argparse
import asyncio
import os
import uuid

from temporalio.client import Client

from sandbox import envfile
from sandbox.contract.names import ORCHESTRATOR_TASK_QUEUE, WORKSPACE_ROOT
from sandbox.orchestrator.workflows import HoldParams, HoldWorkflow


async def main() -> None:
    envfile.load()
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=120)
    args = parser.parse_args()
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    workflow_id = f"hold-{uuid.uuid4().hex[:8]}"
    await client.start_workflow(
        HoldWorkflow.run,
        HoldParams(
            seconds=args.seconds,
            pool=os.environ.get("SANDBOX_POOL", "demo"),
            profile=os.environ.get("SANDBOX_PROFILE", "local"),
            workspace_root=os.environ.get("SANDBOX_WORKSPACE_ROOT", WORKSPACE_ROOT),
        ),
        id=workflow_id,
        task_queue=ORCHESTRATOR_TASK_QUEUE,
    )
    ui = os.environ.get("TEMPORAL_UI", "http://localhost:8233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    print(workflow_id)
    print(f"watch: {ui}/namespaces/{namespace}/workflows/{workflow_id}")


if __name__ == "__main__":
    asyncio.run(main())
