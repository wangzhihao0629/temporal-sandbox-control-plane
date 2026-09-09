"""Sandbox manager worker entry point.

What: connect to Temporal, host acquire and release plus the reconcile
workflow and its activities on the manager queue, and own the reconcile
schedule.
Why: a separate process from the orchestrator so a manager outage never stalls
a running turn, which goes straight to the VM's queue. The worker ensures its
schedule at startup so a fresh checkout gets a running fleet loop with no
console clicks; SANDBOX_RECONCILE_DISABLED=1 pauses an existing schedule
instead, because merely skipping `ensure_schedule` would leave a schedule from
an earlier run firing at a worker started to be quiet.
Production: the sandbox-manager worker on EKS.
"""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from sandbox import envfile
from sandbox.contract.names import MANAGER_TASK_QUEUE
from sandbox.manager.activities import ManagerActivities
from sandbox.manager.reconcile import ReconcileWorkflow
from sandbox.manager.reconcile_activities import ReconcileActivities
from sandbox.manager.reconciler import Reconciler, Tunables
from sandbox.manager.schedule import ensure_schedule, schedule_id_for
from sandbox.registry.client import Registry


def build_provider():
    from sandbox.manager.providers.apple_container import AppleContainerProvider

    return AppleContainerProvider(image=os.environ.get("SANDBOX_VM_IMAGE", "sandbox-vm:dev"))


async def main() -> None:
    envfile.load()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger(__name__)
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    registry = Registry.from_env()
    provider = build_provider()
    profile = os.environ.get("SANDBOX_PROFILE", "local")
    pool = os.environ.get("SANDBOX_POOL", "demo")
    reconciler = Reconciler(registry, provider, Tunables.for_profile(profile))
    worker = Worker(
        client,
        task_queue=MANAGER_TASK_QUEUE,
        workflows=[ReconcileWorkflow],
        activities=[
            *ManagerActivities(registry, provider).all(),
            *ReconcileActivities(reconciler, client).all(),
        ],
        activity_executor=ThreadPoolExecutor(max_workers=8),
    )
    if os.environ.get("SANDBOX_RECONCILE_DISABLED") == "1":
        schedule_id = schedule_id_for(pool)
        try:
            await client.get_schedule_handle(schedule_id).pause(
                note="disabled by SANDBOX_RECONCILE_DISABLED"
            )
            log.info("reconcile schedule %s paused", schedule_id)
        except RPCError as e:
            if e.status != RPCStatusCode.NOT_FOUND:
                raise
            log.info("no reconcile schedule %s to pause", schedule_id)
    else:
        interval = int(os.environ.get("SANDBOX_RECONCILE_INTERVAL_SECONDS", "15"))
        schedule_id = await ensure_schedule(client, pool, interval, profile)
        log.info("reconcile schedule %s every %ss", schedule_id, interval)
    log.info("sandbox manager polling %s", MANAGER_TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
