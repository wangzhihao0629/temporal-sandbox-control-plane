"""Start a SmokeWorkflow from the command line and print the result.

Usage: uv run python -m sandbox.orchestrator.run_smoke
"""

import asyncio
import os
import uuid

from temporalio.client import Client

from sandbox import envfile
from sandbox.contract.names import ORCHESTRATOR_TASK_QUEUE, WORKSPACE_ROOT
from sandbox.orchestrator.workflows import SmokeParams, SmokeWorkflow


async def main() -> None:
    envfile.load()
    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    client = await Client.connect(address, namespace=namespace)
    workflow_id = f"smoke-{uuid.uuid4().hex[:8]}"
    ui = os.environ.get("TEMPORAL_UI", "http://localhost:8233")
    print(f"started {workflow_id}")
    print(f"watch:   {ui}/namespaces/{namespace}/workflows/{workflow_id}")
    result = await client.execute_workflow(
        SmokeWorkflow.run,
        SmokeParams(
            pool=os.environ.get("SANDBOX_POOL", "demo"),
            profile=os.environ.get("SANDBOX_PROFILE", "local"),
            workspace_root=os.environ.get("SANDBOX_WORKSPACE_ROOT", WORKSPACE_ROOT),
        ),
        id=workflow_id,
        task_queue=ORCHESTRATOR_TASK_QUEUE,
    )
    print(f"vm:      {result.vm_id}  (agent {result.agent_version}, lease {result.lease_id})")
    print(f"uname:   {result.uname}")
    print(f"whoami:  {result.whoami}")


if __name__ == "__main__":
    asyncio.run(main())
