"""Reconciler: each step of spec 6.6 against moto and a list-backed provider."""

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import pytest

from sandbox.manager.providers.base import LaunchSpec, ProviderInstance
from sandbox.manager.reconcile_activities import ReconcileActivities
from sandbox.manager.reconcile_types import ReconcileParams
from sandbox.manager.reconciler import Reconciler, Tunables
from sandbox.timeutil import now, now_iso, to_iso


class ListProvider:
    def __init__(self):
        self.instances: dict[str, ProviderInstance] = {}
        self.launched: list[str] = []
        self.terminated: list[str] = []
        self.launch_fails = 0  # raise on this many launches, then behave
        self.terminate_fails = 0

    def launch(self, vm_id, spec: LaunchSpec, env):
        assert spec.image == "img"
        if self.launch_fails:
            self.launch_fails -= 1
            raise RuntimeError("provider is out of capacity")
        self.launched.append(vm_id)
        self.instances[vm_id] = ProviderInstance(vm_id, vm_id, "running", now_iso(), "", "")
        return vm_id

    def terminate(self, ref):
        if self.terminate_fails:
            self.terminate_fails -= 1
            raise RuntimeError("provider refused terminate")
        self.terminated.append(ref)
        self.instances.pop(ref, None)

    def list(self):
        return list(self.instances.values())

    def describe(self, ref):
        return self.instances.get(ref)

    def kill(self, ref):
        self._stop(ref)

    def stop(self, ref):
        self._stop(ref)

    def _stop(self, ref):
        # Like a real provider: a killed or stopped instance is still listed.
        inst = self.instances.get(ref)
        if inst is not None:
            self.instances[ref] = replace(inst, state="stopped")


@dataclass
class FakeClock:
    offset: timedelta = field(default_factory=timedelta)

    def __call__(self) -> datetime:
        return now() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


ACTION_KINDS = {
    "terminate_unknown",
    "write_off_missing",
    "write_off_stopped",
    "write_off_boot",
    "write_off_stale",
    "write_off_stuck",
    "sweep",
    "orphan",
    "inventory_suspect",
    "launch",
    "launch_failed",
    "scale_in",
    "abandon",
}


def _event_types(registry) -> set[str]:
    return {e["type"] for e in registry.recent_events(200) if e["actor"] == "reconciler"}


def _assert_emitted(registry, actions):
    """Every decision the reconciler returns must also reach the event feed."""
    emitted = _event_types(registry)
    for action in actions:
        assert action.kind in emitted, f"no reconciler event for {action.kind}"
    return actions


def _make(registry, min_idle=2, max=5):
    registry.ensure_policy("demo", min_idle=min_idle, max=max, image="img")
    provider = ListProvider()
    clock = FakeClock()
    rec = Reconciler(registry, provider, Tunables.local(), clock=clock)
    return rec, provider, clock


def _vm(registry, provider, vm_id, state="idle"):
    provider.instances[vm_id] = ProviderInstance(vm_id, vm_id, "running", now_iso(), "", "")
    registry.register_vm(vm_id, "demo", vm_id, "test", [1], {})
    if state != "booting":
        assert registry.set_state(vm_id, state, expect="booting")


def _claim(registry, vm_id, owner="wf-1"):
    return registry.claim_idle(
        "demo",
        lease_id=f"lease-{vm_id}",
        request_id=f"req-{vm_id}",
        owner_workflow_id=owner,
        owner_run_id="run",
        hold_seconds=60,
        contract_major=1,
        labels={},
    )


def test_top_up_from_zero_launches_the_floor_once(registry):
    rec, provider, _ = _make(registry)
    actions = _assert_emitted(registry, rec.capacity(rec.inventory("demo")))
    assert [a.kind for a in actions] == ["launch", "launch"]
    assert len(provider.launched) == 2
    # Unregistered instances count as booting: a second pass must not launch again.
    assert rec.capacity(rec.inventory("demo")) == []


def test_a_retried_capacity_step_launches_the_floor_once(registry):
    """The capacity activity takes its own inventory, so a retry cannot double-launch.

    Temporal hands a retry the same argument as the first attempt. If that
    argument were the workflow's snapshot, the retry would see a fleet of zero
    again and launch the floor a second time.
    """
    rec, provider, clock = _make(registry)
    activities = ReconcileActivities(rec, client=None)
    params = ReconcileParams(pool="demo")
    assert [a.kind for a in activities.capacity(params)] == ["launch", "launch"]
    assert activities.capacity(params) == []
    assert len(provider.launched) == 2
    # The same activity also sweeps requests, over that same fresh snapshot.
    registry.record_pending("r1", "demo", "wf")
    clock.advance(Tunables.local().abandon_after_seconds + 1)
    assert [a.kind for a in activities.capacity(params)] == ["launch", "abandon"]


def test_a_failed_launch_ends_the_step_without_aborting_the_pass(registry):
    rec, provider, _ = _make(registry)
    provider.launch_fails = 1
    actions = _assert_emitted(registry, rec.capacity(rec.inventory("demo")))
    # One failure ends the launch loop: the provider is not asked twice in a pass.
    assert [a.kind for a in actions] == ["launch_failed"]
    assert provider.launched == []
    # The next pass still tries, and the floor is reached.
    assert [a.kind for a in rec.capacity(rec.inventory("demo"))] == ["launch", "launch"]


def test_pending_requests_add_to_the_deficit_and_max_caps_it(registry):
    rec, provider, _ = _make(registry, min_idle=2, max=3)
    registry.record_pending("r1", "demo", "wf-1")
    registry.record_pending("r2", "demo", "wf-2")
    actions = rec.capacity(rec.inventory("demo"))
    assert len([a for a in actions if a.kind == "launch"]) == 3


def test_registered_idle_vms_satisfy_the_floor(registry):
    rec, provider, _ = _make(registry)
    _vm(registry, provider, "sbx-a")
    _vm(registry, provider, "sbx-b")
    assert rec.capacity(rec.inventory("demo")) == []


def test_missing_instance_is_written_off_and_later_swept(registry):
    rec, provider, clock = _make(registry)
    _vm(registry, provider, "sbx-a")
    time.sleep(0.002)  # created_at is millisecond-resolution; keep the order strict
    # A survivor: an inventory with nothing in it is suspected, not believed.
    _vm(registry, provider, "sbx-b")
    assert _claim(registry, "sbx-a")["vm_id"] == "sbx-a", "claim_idle takes the oldest"
    provider.instances.pop("sbx-a")
    actions = _assert_emitted(registry, rec.health(rec.inventory("demo")))
    assert [a.kind for a in actions] == ["write_off_missing"]
    row = registry.get_vm("sbx-a")
    assert row["state"] == "terminated" and "lease_id" not in row
    assert rec.health(rec.inventory("demo")) == []
    clock.advance(Tunables.local().sweep_after_seconds + 1)
    swept = _assert_emitted(registry, rec.health(rec.inventory("demo")))
    # The survivor's heartbeat is ten minutes old on the test clock by now.
    assert [a.kind for a in swept] == ["sweep", "write_off_stale"]
    assert registry.get_vm("sbx-a") is None


def test_a_stopped_instance_is_written_off_and_reaped(registry):
    rec, provider, _ = _make(registry)
    _vm(registry, provider, "sbx-a")
    provider.stop("sbx-a")
    actions = _assert_emitted(registry, rec.health(rec.inventory("demo")))
    assert [(a.kind, a.detail) for a in actions] == [("write_off_stopped", "instance stopped")]
    assert registry.get_vm("sbx-a")["state"] == "terminated"
    assert provider.terminated == ["sbx-a"], "a stopped instance is reaped, not just written off"


def test_a_stopped_unknown_instance_needs_no_boot_deadline(registry):
    rec, provider, _ = _make(registry)
    provider.instances["sbx-ghost"] = ProviderInstance(
        "sbx-ghost", "sbx-ghost", "stopped", now_iso(), "", ""
    )
    # Nothing is going to register it, so waiting out the deadline buys nothing.
    actions = _assert_emitted(registry, rec.health(rec.inventory("demo")))
    assert [(a.kind, a.detail) for a in actions] == [
        ("terminate_unknown", "stopped instance without a registry row")
    ]
    assert provider.terminated == ["sbx-ghost"]


def test_a_stopped_instance_is_not_spare_capacity(registry):
    rec, provider, _ = _make(registry)
    provider.instances["sbx-ghost"] = ProviderInstance(
        "sbx-ghost", "sbx-ghost", "stopped", now_iso(), "", ""
    )
    # A running unregistered instance counts as booting; a stopped one counts as nothing.
    assert [a.kind for a in rec.capacity(rec.inventory("demo"))] == ["launch", "launch"]
    counts = rec.sample("demo")
    assert counts["booting"] == 2 and counts["total"] == 2


def test_stale_boot_and_stuck_rows_are_written_off(registry):
    rec, provider, clock = _make(registry)
    _vm(registry, provider, "sbx-idle")
    _vm(registry, provider, "sbx-boot", state="booting")
    _vm(registry, provider, "sbx-stuck")
    assert registry.set_state("sbx-stuck", "recycling", expect="idle")
    clock.advance(Tunables.local().stale_after_seconds + 1)
    kinds = sorted(a.kind for a in _assert_emitted(registry, rec.health(rec.inventory("demo"))))
    assert kinds == ["write_off_stale", "write_off_stale"]  # idle and recycling both went stale
    assert registry.get_vm("sbx-boot")["state"] == "booting", "booting rows get the boot deadline"
    clock.advance(Tunables.local().boot_deadline_seconds)
    booted = _assert_emitted(registry, rec.health(rec.inventory("demo")))
    assert [a.kind for a in booted] == ["write_off_boot"]
    assert set(provider.terminated) == {"sbx-idle", "sbx-stuck", "sbx-boot"}


def test_an_empty_inventory_is_suspected_rather_than_believed(registry):
    rec, provider, _ = _make(registry)
    _vm(registry, provider, "sbx-a")
    _vm(registry, provider, "sbx-b")
    provider.instances.clear()
    actions = _assert_emitted(registry, rec.health(rec.inventory("demo")))
    assert [(a.kind, a.vm_id) for a in actions] == [("inventory_suspect", "")]
    assert sorted(r["state"] for r in registry.list_vms("demo")) == ["idle", "idle"]
    assert provider.terminated == []
    # A provider that answers again gets believed, one VM at a time.
    provider.instances["sbx-a"] = ProviderInstance("sbx-a", "sbx-a", "running", now_iso(), "", "")
    missing = _assert_emitted(registry, rec.health(rec.inventory("demo")))
    assert [(a.kind, a.vm_id) for a in missing] == [("write_off_missing", "sbx-b")]


def test_an_empty_inventory_with_no_live_rows_is_just_an_empty_fleet(registry):
    rec, provider, clock = _make(registry)
    _vm(registry, provider, "sbx-a")
    provider.instances.clear()
    rec.health(rec.inventory("demo"))  # suspected, so sbx-a is still idle
    assert registry.set_state("sbx-a", "terminated", expect="idle")
    # Nothing is live now, so the pass has nothing to protect and sweeps as usual.
    clock.advance(Tunables.local().sweep_after_seconds + 1)
    swept = rec.health(rec.inventory("demo"))
    assert [a.kind for a in swept] == ["sweep"]


def test_a_failed_terminate_is_reported_and_the_write_off_still_lands(registry):
    rec, provider, _ = _make(registry)
    _vm(registry, provider, "sbx-a")
    provider.stop("sbx-a")
    provider.terminate_fails = 1
    actions = _assert_emitted(registry, rec.health(rec.inventory("demo")))
    assert [a.kind for a in actions] == ["write_off_stopped"]
    assert provider.terminated == [], "the provider refused"
    assert "terminate_failed" in _event_types(registry)
    assert registry.get_vm("sbx-a")["state"] == "terminated"


def test_stuck_transition_is_written_off_even_with_a_fresh_heartbeat(registry):
    rec, provider, clock = _make(registry)
    _vm(registry, provider, "sbx-a")
    assert registry.set_state("sbx-a", "draining", expect="idle")
    clock.advance(Tunables.local().stuck_after_seconds + 1)
    # A fresh heartbeat in the test clock's time frame must not save a stuck transition.
    registry.vms.update_item(
        Key={"vm_id": "sbx-a"},
        UpdateExpression="SET last_heartbeat_at = :t",
        ExpressionAttributeValues={":t": to_iso(clock())},
    )
    stuck = _assert_emitted(registry, rec.health(rec.inventory("demo")))
    assert [a.kind for a in stuck] == ["write_off_stuck"]
    assert provider.terminated == ["sbx-a"]


def test_unknown_instance_past_the_boot_deadline_is_terminated(registry):
    rec, provider, clock = _make(registry)
    provider.instances["sbx-ghost"] = ProviderInstance(
        "sbx-ghost", "sbx-ghost", "running", now_iso(), "", ""
    )
    assert rec.health(rec.inventory("demo")) == []
    clock.advance(Tunables.local().boot_deadline_seconds + 1)
    ghosts = _assert_emitted(registry, rec.health(rec.inventory("demo")))
    assert [a.kind for a in ghosts] == ["terminate_unknown"]
    assert provider.terminated == ["sbx-ghost"]


def test_another_pools_vms_are_not_terminated_as_unknown(registry):
    rec, provider, clock = _make(registry)
    registry.ensure_policy("other", min_idle=0, max=5, image="img")
    provider.instances["sbx-other"] = ProviderInstance(
        "sbx-other", "sbx-other", "running", now_iso(), "", ""
    )
    registry.register_vm("sbx-other", "other", "sbx-other", "test", [1], {})
    assert registry.set_state("sbx-other", "idle", expect="booting")
    clock.advance(Tunables.local().boot_deadline_seconds + 1)
    # `provider.list()` is fleet-wide, so the unknown check has to read every pool's rows.
    assert rec.health(rec.inventory("demo")) == []
    assert registry.get_vm("sbx-other")["state"] == "idle"
    assert provider.terminated == []
    # The other pool's instance is registered, so it is not spare capacity for `demo`.
    assert [a.kind for a in rec.capacity(rec.inventory("demo"))] == ["launch", "launch"]


def test_orphaned_leases_are_released_and_running_ones_kept(registry):
    rec, provider, _ = _make(registry)
    for vm_id in ("sbx-a", "sbx-b", "sbx-c"):
        _vm(registry, provider, vm_id)
    # `claim_idle` picks the VM, so the owner is the identity to assert on, not a vm id.
    claimed = {
        owner: _claim(registry, f"sbx-{n}", owner=owner)["vm_id"]
        for owner, n in (("closed", "a"), ("running", "b"), ("vanished", "c"))
    }
    status = {"closed": "TERMINATED", "running": "RUNNING"}
    actions = _assert_emitted(
        registry, rec.leases(rec.inventory("demo"), lambda wf, r: status.get(wf))
    )
    assert sorted(a.vm_id for a in actions) == sorted([claimed["closed"], claimed["vanished"]])
    assert registry.get_vm(claimed["closed"])["state"] == "recycling"
    assert registry.get_vm(claimed["running"])["state"] == "leased"
    assert registry.get_vm(claimed["vanished"])["state"] == "recycling"


def test_expired_lease_with_a_non_running_owner_is_released(registry):
    rec, provider, clock = _make(registry)
    _vm(registry, provider, "sbx-a")
    _claim(registry, "sbx-a", owner="stuck")
    assert rec.leases(rec.inventory("demo"), lambda wf, run: "RUNNING") == []
    clock.advance(61)
    assert rec.leases(rec.inventory("demo"), lambda wf, run: "RUNNING") == []
    orphans = _assert_emitted(
        registry, rec.leases(rec.inventory("demo"), lambda wf, run: "UNKNOWN")
    )
    assert [a.kind for a in orphans] == ["orphan"]


def test_scale_in_retires_the_oldest_unprotected_idle_vm_after_the_cooldown(registry):
    rec, provider, clock = _make(registry, min_idle=1)
    for vm_id in ("sbx-old", "sbx-mid", "sbx-new"):
        _vm(registry, provider, vm_id)
        time.sleep(0.002)  # created_at is millisecond-resolution; keep the order strict
    assert rec.capacity(rec.inventory("demo")) == [], "cooldown not reached"
    clock.advance(Tunables.local().scale_in_cooldown_seconds + 1)
    actions = _assert_emitted(registry, rec.capacity(rec.inventory("demo")))
    assert [a.kind for a in actions] == ["scale_in"] and actions[0].vm_id == "sbx-old"
    assert registry.get_vm("sbx-old")["state"] == "terminated"
    assert provider.terminated == ["sbx-old"]
    registry.record_pending("r1", "demo", "wf")
    # Two idle VMs already cover floor 1 plus one pending request, so nothing launches,
    # and a pending request must also block scale-in.
    assert rec.capacity(rec.inventory("demo")) == [], "pending blocks scale-in"


def test_a_protected_idle_vm_is_never_the_scale_in_victim(registry):
    rec, provider, clock = _make(registry, min_idle=1)
    for vm_id in ("sbx-old", "sbx-mid", "sbx-new"):
        _vm(registry, provider, vm_id)
        time.sleep(0.002)
    registry.vms.update_item(
        Key={"vm_id": "sbx-old"},
        UpdateExpression="SET protected = :p",
        ExpressionAttributeValues={":p": True},
    )
    clock.advance(Tunables.local().scale_in_cooldown_seconds + 1)
    actions = rec.capacity(rec.inventory("demo"))
    assert [a.kind for a in actions] == ["scale_in"] and actions[0].vm_id == "sbx-mid"
    assert registry.get_vm("sbx-old")["state"] == "idle"
    assert provider.terminated == ["sbx-mid"]


def test_abandoned_requests(registry):
    rec, _, clock = _make(registry)
    registry.record_pending("r1", "demo", "wf")
    assert rec.requests(rec.inventory("demo")) == []
    clock.advance(Tunables.local().abandon_after_seconds + 1)
    abandoned = _assert_emitted(registry, rec.requests(rec.inventory("demo")))
    assert [a.kind for a in abandoned] == ["abandon"]
    assert registry.pending_count("demo") == 0


def test_run_records_a_fleet_sample(registry):
    rec, provider, _ = _make(registry)
    _vm(registry, provider, "sbx-a")
    report = rec.run("demo", lambda wf, run: "RUNNING")
    # One VM was launched but not registered, so it counts as booting.
    assert report.counts["idle"] == 1 and report.counts["booting"] == 1
    assert report.counts["min_idle"] == 2 and report.counts["max"] == 5
    assert registry.latest_fleet_sample()["details"]["total"] == 2


def test_every_action_kind_reaches_the_event_feed(registry):
    rec, provider, clock = _make(registry, min_idle=1, max=8)
    t = Tunables.local()
    seen: set[str] = set()

    def step(actions):
        seen.update(a.kind for a in _assert_emitted(registry, actions))

    # launch_failed: the provider refuses the first launch of the pass.
    provider.launch_fails = 1
    step(rec.capacity(rec.inventory("demo")))

    # launch: nothing exists yet, so the floor of 1 is topped up.
    step(rec.capacity(rec.inventory("demo")))

    # orphan: a leased VM whose owner workflow has closed.
    _vm(registry, provider, "sbx-a")
    _claim(registry, "sbx-a", owner="closed")
    step(rec.leases(rec.inventory("demo"), lambda wf, run: "TERMINATED"))

    # write_off_missing: a registered VM whose instance vanished.
    _vm(registry, provider, "sbx-b")
    provider.instances.pop("sbx-b")
    step(rec.health(rec.inventory("demo")))

    # write_off_stopped: a registered VM whose instance is present but not running.
    _vm(registry, provider, "sbx-s")
    provider.stop("sbx-s")
    step(rec.health(rec.inventory("demo")))

    # One pass past every deadline: the launched-but-unregistered VM is unknown,
    # sbx-a is stuck in recycling, sbx-c went stale, and sbx-d never finished booting.
    _vm(registry, provider, "sbx-c")
    _vm(registry, provider, "sbx-d", state="booting")
    clock.advance(t.stuck_after_seconds + 1)
    step(rec.health(rec.inventory("demo")))

    # sweep: the written-off rows age out of the table.
    clock.advance(t.sweep_after_seconds + 1)
    step(rec.health(rec.inventory("demo")))

    # scale_in: two idle VMs against a floor of one, both past the cooldown.
    _vm(registry, provider, "sbx-e")
    time.sleep(0.002)
    _vm(registry, provider, "sbx-f")
    step(rec.capacity(rec.inventory("demo")))

    # abandon: a request that has been pending longer than the deadline.
    registry.record_pending("r1", "demo", "wf")
    step(rec.requests(rec.inventory("demo")))

    # inventory_suspect: the provider answers with nothing while a row is live.
    provider.instances.clear()
    step(rec.health(rec.inventory("demo")))

    assert seen == ACTION_KINDS
    assert _event_types(registry) >= ACTION_KINDS


def test_tunables_profiles():
    assert Tunables.for_profile("local").stale_after_seconds == 30
    assert Tunables.for_profile("prod").boot_deadline_seconds == 600
    assert Tunables.for_profile("test").scale_in_cooldown_seconds == 2
    with pytest.raises(ValueError, match="unknown profile 'nope'"):
        Tunables.for_profile("nope")
