"""The values that cross between the reconcile workflow and its activities.

What: the params, the inventory snapshot, one action, one pass's report, and
the five activity names.
Why: the workflow module must import nothing that pulls boto3 into Temporal's
workflow sandbox, so the shapes it passes to activities live apart from the
reconciler that builds them.
Production: identical.
"""

from dataclasses import dataclass

ACTIVITY_PREFIX = "sandbox.reconcile."
TAKE_INVENTORY = ACTIVITY_PREFIX + "take_inventory"
HEALTH = ACTIVITY_PREFIX + "health"
LEASES = ACTIVITY_PREFIX + "leases"
CAPACITY = ACTIVITY_PREFIX + "capacity"
SAMPLE = ACTIVITY_PREFIX + "sample"


@dataclass
class ReconcileParams:
    pool: str = "demo"
    # Descriptive only: the worker picks its tunables from SANDBOX_PROFILE when it
    # constructs the Reconciler, so changing this on a run changes nothing.
    profile: str = "local"


@dataclass
class Inventory:
    """One snapshot. `rows` is the pool being reconciled; `fleet_rows` is every
    pool's rows, because `provider.list()` is fleet-wide and an instance owned by
    another pool must not look unknown."""

    pool: str
    policy: dict
    rows: list[dict]
    fleet_rows: list[dict]
    instances: list[dict]
    pending: list[dict]
    taken_at: str


@dataclass
class Action:
    kind: str
    vm_id: str
    detail: str


@dataclass
class ReconcileReport:
    pool: str
    counts: dict[str, int]
    actions: list[Action]
