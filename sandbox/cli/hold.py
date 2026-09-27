"""Start a HoldWorkflow and return immediately.

What: start `HoldWorkflow`, which leases a VM and sleeps, and print the
workflow id and a Temporal UI link.
Why: a chaos drill needs a lease that outlives a smoke run — `make terminate
WF=<id>` on the printed id turns it into an orphan for the reconciler to
reclaim.
Production: a CLI or UI action that starts a workflow.

Usage: uv run python -m sandbox.cli.hold --seconds 120
"""

import argparse
import asyncio
import uuid

from sandbox import envfile
from sandbox.cli import common
from sandbox.contract.names import ORCHESTRATOR_TASK_QUEUE
from sandbox.orchestrator.workflows import HoldParams, HoldWorkflow


async def main() -> None:
    envfile.load()
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=120)
    args = parser.parse_args()
    client = await common.connect()
    workflow_id = f"hold-{uuid.uuid4().hex[:8]}"
    await client.start_workflow(
        HoldWorkflow.run,
        HoldParams(seconds=args.seconds, **common.placement()),
        id=workflow_id,
        task_queue=ORCHESTRATOR_TASK_QUEUE,
    )
    print(workflow_id)
    print(f"watch: {common.watch_url(workflow_id)}")


if __name__ == "__main__":
    asyncio.run(main())
