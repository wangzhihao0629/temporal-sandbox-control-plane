"""The reconcile workflow.

What: one pass of the reconciler as a short workflow: inventory, health,
inventory, leases, inventory, capacity, requests, sample.
Why: a Temporal Schedule runs it every few seconds with overlap policy SKIP, so
the fleet loop is durable, visible in the UI, and never runs twice at once.
Nothing here is eternal; each run is a fresh, short history.
Production: identical, on a one-minute schedule.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from sandbox.manager.reconcile_types import (
    CAPACITY,
    HEALTH,
    LEASES,
    REQUESTS,
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
        inv = await workflow.execute_activity(
            TAKE_INVENTORY, params, result_type=Inventory, **_STEP
        )
        actions += await workflow.execute_activity(CAPACITY, inv, result_type=list[Action], **_STEP)
        actions += await workflow.execute_activity(REQUESTS, inv, result_type=list[Action], **_STEP)
        counts = await workflow.execute_activity(
            SAMPLE, params, result_type=dict[str, int], **_STEP
        )
        return ReconcileReport(pool=params.pool, counts=counts, actions=actions)
