"""Start a SmokeWorkflow from the command line and print the result.

What: connect to Temporal, start SmokeWorkflow, print the vm, agent version,
lease, and the two command outputs it collected.
Why: a one-shot way to prove the manager and orchestrator workers, and the VM
they lease, are wired together end to end.
Production: a CLI or UI action that starts a workflow.

Usage: uv run python -m sandbox.cli.smoke
"""

import asyncio
import uuid

from sandbox import envfile
from sandbox.cli import common
from sandbox.contract.names import ORCHESTRATOR_TASK_QUEUE
from sandbox.orchestrator.workflows import SmokeParams, SmokeWorkflow


async def main() -> None:
    envfile.load()
    client = await common.connect()
    workflow_id = f"smoke-{uuid.uuid4().hex[:8]}"
    print(f"started {workflow_id}")
    print(f"watch:   {common.watch_url(workflow_id)}")
    result = await client.execute_workflow(
        SmokeWorkflow.run,
        SmokeParams(**common.placement()),
        id=workflow_id,
        task_queue=ORCHESTRATOR_TASK_QUEUE,
    )
    print(f"vm:      {result.vm_id}  (agent {result.agent_version}, lease {result.lease_id})")
    print(f"uname:   {result.uname}")
    print(f"whoami:  {result.whoami}")


if __name__ == "__main__":
    asyncio.run(main())
