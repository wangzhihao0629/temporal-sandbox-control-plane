"""Run one reconcile pass now and print what it did.

What: start `ReconcileWorkflow` once, off the schedule, and print its actions
and counts.
Why: a demo or a chaos drill needs the reconciler's reaction now, not after
the interval next fires.
Production: not deployed; production always runs on the Schedule.

Usage: uv run python -m sandbox.manager.reconcile_once
"""

import asyncio
import os
import uuid

from temporalio.client import Client

from sandbox import envfile
from sandbox.contract.names import MANAGER_TASK_QUEUE
from sandbox.manager.reconcile import ReconcileWorkflow
from sandbox.manager.reconcile_types import ReconcileParams


async def main() -> None:
    envfile.load()
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    params = ReconcileParams(
        pool=os.environ.get("SANDBOX_POOL", "demo"),
        profile=os.environ.get("SANDBOX_PROFILE", "local"),
    )
    report = await client.execute_workflow(
        ReconcileWorkflow.run,
        params,
        id=f"reconcile-once-{uuid.uuid4().hex[:8]}",
        task_queue=MANAGER_TASK_QUEUE,
    )
    for action in report.actions:
        print(f"  {action.kind:<20} {action.vm_id:<16} {action.detail}")
    if not report.actions:
        print("  no actions")
    print("  counts:", ", ".join(f"{k}={v}" for k, v in report.counts.items()))


if __name__ == "__main__":
    asyncio.run(main())
