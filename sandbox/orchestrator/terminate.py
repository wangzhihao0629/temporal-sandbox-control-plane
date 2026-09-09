"""Terminate a workflow by id, the way an operator would from the UI.

What: connect to Temporal and terminate the given workflow id.
Why: terminate, unlike cancel, skips the workflow's finally block, so a held
lease becomes an orphan for the reconciler to reclaim — the chaos drill for
the orphan path.
Production: the same action from the Temporal UI or CLI.

Usage: uv run python -m sandbox.orchestrator.terminate <workflow_id>
"""

import asyncio
import os
import sys

from temporalio.client import Client

from sandbox import envfile


async def main() -> None:
    envfile.load()
    if len(sys.argv) != 2:
        sys.exit("usage: uv run python -m sandbox.orchestrator.terminate <workflow_id>")
    workflow_id = sys.argv[1]
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    await client.get_workflow_handle(workflow_id).terminate("chaos drill")
    print(f"terminated {workflow_id}")


if __name__ == "__main__":
    asyncio.run(main())
