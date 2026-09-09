"""The reconciler: spec section 6.6 as a pure class.

What: five steps over one inventory snapshot, health, leases, capacity,
requests, sample, each returning the actions it took.
Why: the loop that owns the fleet must be readable and unit-testable without
Temporal or containers. Temporal adds durability and scheduling around it, the
provider adds machines under it, and neither leaks in. Every decision is an
Action that also lands in the event feed, so the dashboard can show why a VM
appeared or disappeared.
Production: identical; the tunables grow and the provider is EC2.
"""

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass

from sandbox.manager.launch import new_vm_id, vm_environment
from sandbox.manager.policy import PoolPolicy
from sandbox.registry.client import Registry
from sandbox.timeutil import now, now_iso, parse_iso

log = logging.getLogger(__name__)

NAME_PREFIX = "sbx-"
LIVE_STATES = ("booting", "idle", "leased", "recycling", "draining", "terminating")
WRITTEN_OFF_STATES = ("terminated", "dead")
TRANSITION_STATES = ("recycling", "draining", "terminating")
CLOSED_WORKFLOW_STATUSES = {
    "COMPLETED",
    "FAILED",
    "CANCELED",
    "TERMINATED",
    "TIMED_OUT",
    "CONTINUED_AS_NEW",
}

OwnerStatus = Callable[[str, str], str | None]


@dataclass(frozen=True)
class Tunables:
    boot_deadline_seconds: int
    stale_after_seconds: int
    stuck_after_seconds: int
    scale_in_cooldown_seconds: int
    abandon_after_seconds: int
    sweep_after_seconds: int

    @classmethod
    def local(cls) -> "Tunables":
        return cls(120, 30, 300, 120, 600, 600)

    @classmethod
    def prod(cls) -> "Tunables":
        return cls(600, 180, 300, 600, 600, 3600)

    @classmethod
    def test(cls) -> "Tunables":
        return cls(20, 5, 10, 2, 5, 2)

    @classmethod
    def for_profile(cls, name: str) -> "Tunables":
        return {"local": cls.local, "prod": cls.prod, "test": cls.test}[name]()


@dataclass
class Inventory:
    pool: str
    policy: dict
    rows: list[dict]
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


class Reconciler:
    def __init__(self, registry: Registry, provider, tunables: Tunables, clock=now, launcher=None):
        self.registry = registry
        self.provider = provider
        self.t = tunables
        self.clock = clock
        self._launcher = launcher or self._launch

    # ---- snapshot ------------------------------------------------------------------

    def inventory(self, pool: str) -> Inventory:
        policy = self.registry.get_policy(pool)
        if policy is None:
            raise RuntimeError(f"pool {pool!r} has no policy item; run sandbox.bootstrap")
        rows = self.registry.list_vms(pool)
        instances = [asdict(i) for i in self.provider.list() if i.vm_id.startswith(NAME_PREFIX)]
        pending = self.registry.pending_requests(pool)
        return Inventory(pool, policy, rows, instances, pending, now_iso())

    def _age(self, iso: str) -> float:
        return (self.clock() - parse_iso(iso)).total_seconds()

    @staticmethod
    def _ref(row: dict) -> str:
        return row.get("provider_ref", row["vm_id"])

    # ---- step 1: health -------------------------------------------------------------

    def health(self, inv: Inventory) -> list[Action]:
        actions: list[Action] = []
        known_refs = {self._ref(r) for r in inv.rows}
        live_refs = {i["provider_ref"] for i in inv.instances}

        for inst in inv.instances:
            if inst["provider_ref"] in known_refs:
                continue
            if inst["created_at"] and self._age(inst["created_at"]) > self.t.boot_deadline_seconds:
                self._terminate(inst["provider_ref"])
                actions.append(
                    Action(
                        "terminate_unknown",
                        inst["vm_id"],
                        "no registry row past the boot deadline",
                    )
                )

        for row in inv.rows:
            vm_id, state, ref = row["vm_id"], row["state"], self._ref(row)
            if state in WRITTEN_OFF_STATES:
                if self._age(row["last_transition_at"]) > self.t.sweep_after_seconds:
                    self.registry.delete_vm(vm_id)
                    actions.append(Action("sweep", vm_id, f"{state} row swept"))
                continue
            if ref not in live_refs:
                self._write_off(vm_id, "instance missing", "write_off_missing", actions)
                continue
            if state == "booting":
                if self._age(row["created_at"]) > self.t.boot_deadline_seconds:
                    self._terminate(ref)
                    self._write_off(vm_id, "boot deadline exceeded", "write_off_boot", actions)
                continue
            if state in TRANSITION_STATES and (
                self._age(row["last_transition_at"]) > self.t.stuck_after_seconds
            ):
                self._terminate(ref)
                self._write_off(vm_id, f"stuck in {state}", "write_off_stuck", actions)
                continue
            if self._age(row["last_heartbeat_at"]) > self.t.stale_after_seconds:
                self._terminate(ref)
                self._write_off(vm_id, "stale heartbeat", "write_off_stale", actions)
        return actions

    # ---- step 2: leases -------------------------------------------------------------

    def leases(self, inv: Inventory, owner_status: OwnerStatus) -> list[Action]:
        actions: list[Action] = []
        for row in inv.rows:
            if row["state"] != "leased" or "lease_id" not in row:
                continue
            status = owner_status(row["owner_workflow_id"], row.get("owner_run_id", ""))
            expired = bool(row.get("lease_expires_at")) and self._age(row["lease_expires_at"]) > 0
            orphaned = (
                status is None
                or status in CLOSED_WORKFLOW_STATUSES
                or (expired and status != "RUNNING")
            )
            if not orphaned:
                continue
            if self.registry.release(row["vm_id"], row["lease_id"], "recycle"):
                detail = f"owner {row['owner_workflow_id']} is {status or 'gone'}"
                self.registry.emit(
                    "orphan",
                    "reconciler",
                    f"released orphaned lease on {row['vm_id']}: {detail}",
                    vm_id=row["vm_id"],
                )
                actions.append(Action("orphan", row["vm_id"], detail))
        return actions

    # ---- step 3: capacity -----------------------------------------------------------

    def capacity(self, inv: Inventory) -> list[Action]:
        actions: list[Action] = []
        policy = PoolPolicy.from_row(inv.policy)
        live = [r for r in inv.rows if r["state"] in LIVE_STATES]
        known_refs = {self._ref(r) for r in inv.rows}
        unregistered = [i for i in inv.instances if i["provider_ref"] not in known_refs]
        idle = [r for r in live if r["state"] == "idle"]
        booting = [r for r in live if r["state"] == "booting"]
        total = len(live) + len(unregistered)
        available = len(idle) + len(booting) + len(unregistered)
        deficit = policy.min_idle + len(inv.pending) - available
        room = policy.max - total

        for _ in range(max(0, min(deficit, room))):
            vm_id = new_vm_id()
            self._launcher(vm_id, inv.pool, policy)
            self.registry.emit(
                "launch",
                "reconciler",
                f"launched {vm_id}",
                vm_id=vm_id,
                details={"deficit": deficit, "pending": len(inv.pending)},
            )
            actions.append(
                Action("launch", vm_id, f"deficit {deficit}, pending {len(inv.pending)}")
            )

        if deficit <= 0 and not inv.pending and len(idle) > policy.min_idle:
            candidates = sorted(
                (
                    r
                    for r in idle
                    if not r.get("protected")
                    and self._age(r["last_transition_at"]) > self.t.scale_in_cooldown_seconds
                ),
                key=lambda r: r["created_at"],
            )
            if candidates:
                victim = candidates[0]
                vm_id = victim["vm_id"]
                if self.registry.set_state(vm_id, "terminating", expect="idle", reason="scale-in"):
                    self._terminate(self._ref(victim))
                    self.registry.write_off(vm_id, "scale-in")
                    self.registry.emit(
                        "scale_in", "reconciler", f"retired surplus {vm_id}", vm_id=vm_id
                    )
                    actions.append(
                        Action("scale_in", vm_id, f"idle {len(idle)} above floor {policy.min_idle}")
                    )
        return actions

    # ---- step 4: requests -----------------------------------------------------------

    def requests(self, inv: Inventory) -> list[Action]:
        actions: list[Action] = []
        for req in inv.pending:
            if self._age(req["created_at"]) > self.t.abandon_after_seconds:
                if self.registry.abandon_request(req["request_id"]):
                    actions.append(
                        Action("abandon", "", f"request {req['request_id']} pending too long")
                    )
        return actions

    # ---- step 5: sample -------------------------------------------------------------

    def sample(self, pool: str) -> dict[str, int]:
        inv = self.inventory(pool)
        policy = PoolPolicy.from_row(inv.policy)
        known_refs = {self._ref(r) for r in inv.rows}
        unregistered = sum(1 for i in inv.instances if i["provider_ref"] not in known_refs)
        by_state = Counter(r["state"] for r in inv.rows)
        counts = {
            "total": sum(by_state[s] for s in LIVE_STATES) + unregistered,
            "idle": by_state["idle"],
            "leased": by_state["leased"],
            "booting": by_state["booting"] + unregistered,
            "recycling": by_state["recycling"],
            "draining": by_state["draining"],
            "dead": by_state["terminated"] + by_state["dead"],
            "pending": len(inv.pending),
            "min_idle": policy.min_idle,
            "max": policy.max,
        }
        self.registry.emit(
            "fleet_sample",
            "reconciler",
            f"{counts['idle']} idle / {counts['leased']} leased / {counts['total']} total",
            details=counts,
        )
        return counts

    # ---- the whole pass -------------------------------------------------------------

    def run(self, pool: str, owner_status: OwnerStatus) -> ReconcileReport:
        actions = self.health(self.inventory(pool))
        actions += self.leases(self.inventory(pool), owner_status)
        inv = self.inventory(pool)
        actions += self.capacity(inv)
        actions += self.requests(inv)
        return ReconcileReport(pool, self.sample(pool), actions)

    # ---- helpers ----------------------------------------------------------------------

    def _terminate(self, ref: str) -> None:
        try:
            self.provider.terminate(ref)
        except Exception as e:  # noqa: BLE001 - a failed terminate must not stop the pass
            log.warning("terminate %s failed: %s", ref, e)

    def _write_off(self, vm_id: str, reason: str, kind: str, actions: list[Action]) -> None:
        if self.registry.write_off(vm_id, reason):
            self.registry.emit(kind, "reconciler", f"{vm_id}: {reason}", vm_id=vm_id)
            actions.append(Action(kind, vm_id, reason))

    def _launch(self, vm_id: str, pool: str, policy: PoolPolicy) -> None:
        self.provider.launch(vm_id, policy.launch_spec(), vm_environment(vm_id, pool))
