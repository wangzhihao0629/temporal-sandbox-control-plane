"""The reconciler: spec section 6.6 as a pure class.

What: five steps over one inventory snapshot, health, leases, capacity,
requests, sample, each returning the actions it took.
Why: the loop that owns the fleet must be readable and unit-testable without
Temporal or containers. Temporal adds durability and scheduling around it, the
provider adds machines under it, and neither leaks in. Every decision is an
Action that also lands in the event feed, so the dashboard can show why a VM
appeared or disappeared. Only a running instance counts as alive: a stopped one
answers `list` but runs no agent, so it can neither back a registry row nor
stand in for capacity. A running instance the provider knows about but no
registry row claims cannot yet be attributed to a pool, so capacity counts every
unregistered instance toward the pool being reconciled: a single-pool
simplification, and the reason the unknown-instance check reads the whole
fleet's rows rather than one pool's.
Production: identical; the tunables grow and the provider is EC2.
"""

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass

from sandbox.manager.launch import new_vm_id, vm_environment
from sandbox.manager.policy import PoolPolicy
from sandbox.manager.reconcile_types import Action, Inventory, ReconcileReport
from sandbox.registry.client import Registry
from sandbox.timeutil import now, parse_iso, to_iso

log = logging.getLogger(__name__)

NAME_PREFIX = "sbx-"
RUNNING = "running"  # the one provider-reported instance state that backs a live VM
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
        profiles = {"local": cls.local, "prod": cls.prod, "test": cls.test}
        if name not in profiles:
            raise ValueError(f"unknown profile {name!r}")
        return profiles[name]()


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
        # One scan, two views: a second `list_vms(pool)` would be a second scan of
        # the same table and could disagree with this one.
        fleet_rows = self.registry.list_vms()
        rows = [r for r in fleet_rows if r["pool"] == pool]
        instances = [asdict(i) for i in self.provider.list() if i.vm_id.startswith(NAME_PREFIX)]
        pending = self.registry.pending_requests(pool)
        return Inventory(
            pool=pool,
            policy=policy,
            rows=rows,
            fleet_rows=fleet_rows,
            instances=instances,
            pending=pending,
            taken_at=to_iso(self.clock()),
        )

    def _age(self, iso: str) -> float:
        return (self.clock() - parse_iso(iso)).total_seconds()

    @staticmethod
    def _ref(row: dict) -> str:
        return row.get("provider_ref", row["vm_id"])

    # ---- step 1: health -------------------------------------------------------------

    def health(self, inv: Inventory) -> list[Action]:
        actions: list[Action] = []
        known_refs = {self._ref(r) for r in inv.fleet_rows}
        live_refs = {i["provider_ref"] for i in inv.instances if i["state"] == RUNNING}
        stopped_refs = {i["provider_ref"] for i in inv.instances} - live_refs

        for inst in inv.instances:
            ref = inst["provider_ref"]
            if ref in known_refs:
                continue
            # A stopped orphan needs no deadline: nothing is going to register it.
            stopped = ref in stopped_refs
            aged = bool(inst["created_at"]) and (
                self._age(inst["created_at"]) > self.t.boot_deadline_seconds
            )
            if not (stopped or aged):
                continue
            detail = (
                "stopped instance without a registry row"
                if stopped
                else "no registry row past the boot deadline"
            )
            self._terminate(ref)
            self.registry.emit(
                "terminate_unknown",
                "reconciler",
                f"terminated {inst['vm_id']}: {detail}",
                vm_id=inst["vm_id"],
            )
            actions.append(Action("terminate_unknown", inst["vm_id"], detail))

        for row in inv.rows:
            vm_id, state, ref = row["vm_id"], row["state"], self._ref(row)
            if state in WRITTEN_OFF_STATES:
                if self._age(row["last_transition_at"]) > self.t.sweep_after_seconds:
                    self.registry.delete_vm(vm_id)
                    self.registry.emit(
                        "sweep", "reconciler", f"{vm_id}: {state} row swept", vm_id=vm_id
                    )
                    actions.append(Action("sweep", vm_id, f"{state} row swept"))
                continue
            if ref not in live_refs:
                if ref in stopped_refs:
                    self._terminate(ref)
                    self._write_off(vm_id, "instance stopped", "write_off_stopped", actions)
                else:
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
            owner = row.get("owner_workflow_id")
            if not owner:
                log.warning("leased row %s has no owner_workflow_id; skipping", row["vm_id"])
                continue
            status = owner_status(owner, row.get("owner_run_id", ""))
            expired = bool(row.get("lease_expires_at")) and self._age(row["lease_expires_at"]) > 0
            orphaned = (
                status is None
                or status in CLOSED_WORKFLOW_STATUSES
                or (expired and status != "RUNNING")
            )
            if not orphaned:
                continue
            if self.registry.release(row["vm_id"], row["lease_id"], "recycle"):
                detail = f"owner {owner} is {status or 'gone'}"
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
        known_refs = {self._ref(r) for r in inv.fleet_rows}
        unregistered = [
            i
            for i in inv.instances
            if i["provider_ref"] not in known_refs and i["state"] == RUNNING
        ]
        idle = [r for r in live if r["state"] == "idle"]
        booting = [r for r in live if r["state"] == "booting"]
        total = len(live) + len(unregistered)
        available = len(idle) + len(booting) + len(unregistered)
        deficit = policy.min_idle + len(inv.pending) - available
        room = policy.max - total

        for _ in range(max(0, min(deficit, room))):
            vm_id = new_vm_id()
            try:
                self._launcher(vm_id, inv.pool, policy)
            # Broad on purpose: a provider that refuses one launch must not abort
            # the pass, and must not be asked again in it.
            except Exception as e:
                log.warning("launch %s failed: %s", vm_id, e)
                self.registry.emit(
                    "launch_failed",
                    "reconciler",
                    f"launch {vm_id} failed: {e}",
                    vm_id=vm_id,
                    details={"error": str(e)},
                )
                actions.append(Action("launch_failed", vm_id, str(e)))
                break
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
                    detail = f"request {req['request_id']} pending too long"
                    self.registry.emit(
                        "abandon",
                        "reconciler",
                        detail,
                        vm_id="",
                        details={"request_id": req["request_id"]},
                    )
                    actions.append(Action("abandon", "", detail))
        return actions

    # ---- step 5: sample -------------------------------------------------------------

    def sample(self, pool: str) -> dict[str, int]:
        inv = self.inventory(pool)
        policy = PoolPolicy.from_row(inv.policy)
        known_refs = {self._ref(r) for r in inv.fleet_rows}
        unregistered = sum(
            1
            for i in inv.instances
            if i["provider_ref"] not in known_refs and i["state"] == RUNNING
        )
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
        # Broad on purpose: one provider failure must not abort the rest of the pass.
        except Exception as e:
            log.warning("terminate %s failed: %s", ref, e)
            self.registry.emit(
                "terminate_failed",
                "reconciler",
                f"terminate {ref} failed: {e}",
                vm_id=ref,
                details={"error": str(e)},
            )

    def _write_off(self, vm_id: str, reason: str, kind: str, actions: list[Action]) -> None:
        if self.registry.write_off(vm_id, reason):
            self.registry.emit(kind, "reconciler", f"{vm_id}: {reason}", vm_id=vm_id)
            actions.append(Action(kind, vm_id, reason))

    def _launch(self, vm_id: str, pool: str, policy: PoolPolicy) -> None:
        self.provider.launch(vm_id, policy.launch_spec(), vm_environment(vm_id, pool))
