"""Terminate a workflow by id, the way an operator would from the UI.

What: connect to Temporal and terminate the given workflow id.
Why: terminate, unlike cancel, skips the workflow's finally block, so a held
lease becomes an orphan for the reconciler to reclaim — the chaos drill for
the orphan path.
Production: the same action from the Temporal UI or CLI.

Usage: uv run python -m sandbox.cli.terminate <workflow_id>
"""

import asyncio
import sys

from sandbox import envfile
from sandbox.cli import common


async def main() -> None:
    envfile.load()
    if len(sys.argv) != 2:
        sys.exit("usage: uv run python -m sandbox.cli.terminate <workflow_id>")
    workflow_id = sys.argv[1]
    client = await common.connect()
    await client.get_workflow_handle(workflow_id).terminate("chaos drill")
    print(f"terminated {workflow_id}")


if __name__ == "__main__":
    asyncio.run(main())
