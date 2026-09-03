"""Sandbox manager worker entry point.

What: connect to Temporal, host acquire and release on the manager queue.
Why: a separate process from the orchestrator so a manager outage never stalls
a running turn, which goes straight to the VM's queue.
Production: the sandbox-manager worker on EKS. Task 9 wires in the provider;
Plan 2 adds the reconciler schedule.
"""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from sandbox import envfile
from sandbox.contract.names import MANAGER_TASK_QUEUE
from sandbox.manager.activities import ManagerActivities
from sandbox.registry.client import Registry


def build_provider():
    """Task 9 replaces this with the Apple container provider."""
    return None


async def main() -> None:
    envfile.load()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    activities = ManagerActivities(Registry.from_env(), build_provider())
    worker = Worker(
        client,
        task_queue=MANAGER_TASK_QUEUE,
        activities=activities.all(),
        activity_executor=ThreadPoolExecutor(max_workers=8),
    )
    logging.getLogger(__name__).info("sandbox manager polling %s", MANAGER_TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
