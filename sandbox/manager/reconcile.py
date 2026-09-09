"""The reconcile workflow.

What: one pass of the reconciler as a short workflow: inventory, health,
inventory, leases, capacity, sample.
Why: a Temporal Schedule runs it every few seconds with overlap policy SKIP, so
the fleet loop is durable, visible in the UI, and the schedule never overlaps
its own runs. Capacity takes the params rather than an inventory: it snapshots
the fleet itself, so a retry of the one step that creates machines cannot launch
against a stale count. Nothing here is eternal; each run is a fresh, short
history.
Production: identical, on a one-minute schedule.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from sandbox.manager.reconcile_types import (
    CAPACITY,
    HEALTH,
    LEASES,
    SAMPLE,
    TAKE_INVENTORY,
    Action,
    Inventory,
    ReconcileParams,
    ReconcileReport,
)

_STEP = dict(
    start_to_close_timeout=timedelta(minutes=2),
    retry_policy=RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=1)),
)
# Capacity is the only step that starts machines, and starting one is slow. Its
# budget is one inventory `container ls` plus a launch for every slot the pool
# can hold: CLI_TIMEOUT_SECONDS + max * LAUNCH_TIMEOUT_SECONDS = 60 + 5 * 180 =
# 960 seconds. A shorter timeout would let a slow launch outlive its activity,
# and the retry would then run beside a `container run` that is still going.
_CAPACITY_STEP = {**_STEP, "start_to_close_timeout": timedelta(minutes=16)}


@workflow.defn
class ReconcileWorkflow:
    @workflow.run
    async def run(self, params: ReconcileParams) -> ReconcileReport:
        actions: list[Action] = []
        inv = await workflow.execute_activity(
            TAKE_INVENTORY, params, result_type=Inventory, **_STEP
        )
        actions += await workflow.execute_activity(HEALTH, inv, result_type=list[Action], **_STEP)
        inv = await workflow.execute_activity(
            TAKE_INVENTORY, params, result_type=Inventory, **_STEP
        )
        actions += await workflow.execute_activity(LEASES, inv, result_type=list[Action], **_STEP)
        actions += await workflow.execute_activity(
            CAPACITY, params, result_type=list[Action], **_CAPACITY_STEP
        )
        counts = await workflow.execute_activity(
            SAMPLE, params, result_type=dict[str, int], **_STEP
        )
        return ReconcileReport(pool=params.pool, counts=counts, actions=actions)
