"""The reconcile schedule.

What: create or update the Temporal Schedule that runs `ReconcileWorkflow`
for a pool at a fixed interval with overlap policy SKIP.
Why: the manager worker calls this at startup so a fresh checkout gets a
running fleet loop with no console clicks; calling it again with a new
interval updates the schedule in place.
Production: the same call from the deployed worker, or Terraform.
"""

import dataclasses
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)

from sandbox.contract.names import MANAGER_TASK_QUEUE
from sandbox.manager.reconcile import ReconcileWorkflow
from sandbox.manager.reconcile_types import ReconcileParams


def schedule_id_for(pool: str) -> str:
    return f"sandbox-reconcile-{pool}"


def _schedule(pool: str, interval_seconds: int, profile: str) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            ReconcileWorkflow.run,
            ReconcileParams(pool=pool, profile=profile),
            id=f"reconcile-{pool}",
            task_queue=MANAGER_TASK_QUEUE,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(seconds=interval_seconds))]
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


async def ensure_schedule(client: Client, pool: str, interval_seconds: int, profile: str) -> str:
    schedule_id = schedule_id_for(pool)
    desired = _schedule(pool, interval_seconds, profile)
    try:
        await client.create_schedule(schedule_id, desired)
    except ScheduleAlreadyRunningError:
        handle = client.get_schedule_handle(schedule_id)

        async def _update(inp: ScheduleUpdateInput) -> ScheduleUpdate:
            # Carry the live state across: a default ScheduleState would un-pause a
            # schedule an operator paused and drop the note saying why.
            state = inp.description.schedule.state
            return ScheduleUpdate(schedule=dataclasses.replace(desired, state=state))

        await handle.update(_update)
    return schedule_id
