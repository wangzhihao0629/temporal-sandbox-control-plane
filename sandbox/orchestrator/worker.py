"""Orchestrator worker entry point.

What: connect to Temporal, host the orchestrator's workflows on its queue.
Why: a separate process from the manager so a manager outage never stalls a
workflow already mid-turn, and so each queue scales on its own workload.
Production: the sandbox-orchestrator worker on EKS.
"""

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from sandbox import envfile
from sandbox.contract.names import ORCHESTRATOR_TASK_QUEUE
from sandbox.objectstore import ObjectStore
from sandbox.orchestrator.activities import OrchestratorActivities
from sandbox.orchestrator.workflows import CodingSessionDemoWorkflow, HoldWorkflow, SmokeWorkflow

WORKFLOWS = [SmokeWorkflow, HoldWorkflow, CodingSessionDemoWorkflow]


async def main() -> None:
    envfile.load()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    worker = Worker(
        client,
        task_queue=ORCHESTRATOR_TASK_QUEUE,
        workflows=WORKFLOWS,
        activities=OrchestratorActivities(ObjectStore.from_env()).all(),
    )
    logging.getLogger(__name__).info("orchestrator polling %s", ORCHESTRATOR_TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
