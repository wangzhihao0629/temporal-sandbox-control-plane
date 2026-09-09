"""Reconciler steps as Temporal activities.

What: one activity per step of the pure `Reconciler`, plus the owner-status
lookup that needs a Temporal client.
Why: each step is its own activity so the Temporal UI shows what a pass did and
a failing step retries alone. The leases step is async because describing a
workflow is an awaitable client call; the others are sync and run in the
manager's thread pool because the registry and provider are blocking.
Production: identical.
"""

import asyncio

from temporalio import activity
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

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
)
from sandbox.manager.reconciler import Reconciler


class ReconcileActivities:
    def __init__(self, reconciler: Reconciler, client: Client) -> None:
        self.reconciler = reconciler
        self.client = client

    def all(self) -> list:
        return [
            self.take_inventory,
            self.health,
            self.leases,
            self.capacity,
            self.requests,
            self.sample,
        ]

    @activity.defn(name=TAKE_INVENTORY)
    def take_inventory(self, params: ReconcileParams) -> Inventory:
        return self.reconciler.inventory(params.pool)

    @activity.defn(name=HEALTH)
    def health(self, inv: Inventory) -> list[Action]:
        return self.reconciler.health(inv)

    @activity.defn(name=LEASES)
    async def leases(self, inv: Inventory) -> list[Action]:
        statuses: dict[tuple[str, str], str | None] = {}
        for row in inv.rows:
            if row.get("state") == "leased" and row.get("owner_workflow_id"):
                key = (row["owner_workflow_id"], row.get("owner_run_id", ""))
                if key not in statuses:
                    statuses[key] = await self._owner_status(*key)
        return await asyncio.to_thread(
            self.reconciler.leases, inv, lambda wf, run: statuses.get((wf, run))
        )

    async def _owner_status(self, workflow_id: str, run_id: str) -> str | None:
        try:
            handle = self.client.get_workflow_handle(workflow_id, run_id=run_id or None)
            desc = await handle.describe()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return None
            raise
        return desc.status.name if desc.status is not None else None

    @activity.defn(name=CAPACITY)
    def capacity(self, inv: Inventory) -> list[Action]:
        return self.reconciler.capacity(inv)

    @activity.defn(name=REQUESTS)
    def requests(self, inv: Inventory) -> list[Action]:
        return self.reconciler.requests(inv)

    @activity.defn(name=SAMPLE)
    def sample(self, params: ReconcileParams) -> dict[str, int]:
        return self.reconciler.sample(params.pool)
