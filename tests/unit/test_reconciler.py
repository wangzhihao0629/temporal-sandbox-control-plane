"""Reconciler: each step of spec 6.6 against moto and a list-backed provider."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sandbox.manager.providers.base import LaunchSpec, ProviderInstance
from sandbox.manager.reconciler import Reconciler, Tunables
from sandbox.timeutil import now, now_iso, to_iso


class ListProvider:
    def __init__(self):
        self.instances: dict[str, ProviderInstance] = {}
        self.launched: list[str] = []
        self.terminated: list[str] = []

    def launch(self, vm_id, spec: LaunchSpec, env):
        assert spec.image == "img"
        self.launched.append(vm_id)
        self.instances[vm_id] = ProviderInstance(vm_id, vm_id, "running", now_iso(), "", "")
        return vm_id

    def terminate(self, ref):
        self.terminated.append(ref)
        self.instances.pop(ref, None)

    def list(self):
        return list(self.instances.values())

    def describe(self, ref):
        return self.instances.get(ref)

    def kill(self, ref):
        self.instances.pop(ref, None)

    def stop(self, ref):
        self.instances.pop(ref, None)


@dataclass
class FakeClock:
    offset: timedelta = field(default_factory=timedelta)

    def __call__(self) -> datetime:
        return now() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


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
    actions = rec.capacity(rec.inventory("demo"))
    assert [a.kind for a in actions] == ["launch", "launch"]
    assert len(provider.launched) == 2
    # Unregistered instances count as booting: a second pass must not launch again.
    assert rec.capacity(rec.inventory("demo")) == []


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
    _claim(registry, "sbx-a")
    provider.instances.pop("sbx-a")
    actions = rec.health(rec.inventory("demo"))
    assert [a.kind for a in actions] == ["write_off_missing"]
    row = registry.get_vm("sbx-a")
    assert row["state"] == "terminated" and "lease_id" not in row
    assert rec.health(rec.inventory("demo")) == []
    clock.advance(Tunables.local().sweep_after_seconds + 1)
    assert [a.kind for a in rec.health(rec.inventory("demo"))] == ["sweep"]
    assert registry.get_vm("sbx-a") is None


def test_stale_boot_and_stuck_rows_are_written_off(registry):
    rec, provider, clock = _make(registry)
    _vm(registry, provider, "sbx-idle")
    _vm(registry, provider, "sbx-boot", state="booting")
    _vm(registry, provider, "sbx-stuck")
    assert registry.set_state("sbx-stuck", "recycling", expect="idle")
    clock.advance(Tunables.local().stale_after_seconds + 1)
    kinds = sorted(a.kind for a in rec.health(rec.inventory("demo")))
    assert kinds == ["write_off_stale", "write_off_stale"]  # idle and recycling both went stale
    assert registry.get_vm("sbx-boot")["state"] == "booting", "booting rows get the boot deadline"
    clock.advance(Tunables.local().boot_deadline_seconds)
    assert [a.kind for a in rec.health(rec.inventory("demo"))] == ["write_off_boot"]
    assert set(provider.terminated) == {"sbx-idle", "sbx-stuck", "sbx-boot"}


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
    assert [a.kind for a in rec.health(rec.inventory("demo"))] == ["write_off_stuck"]
    assert provider.terminated == ["sbx-a"]


def test_unknown_instance_past_the_boot_deadline_is_terminated(registry):
    rec, provider, clock = _make(registry)
    provider.instances["sbx-ghost"] = ProviderInstance(
        "sbx-ghost", "sbx-ghost", "running", now_iso(), "", ""
    )
    assert rec.health(rec.inventory("demo")) == []
    clock.advance(Tunables.local().boot_deadline_seconds + 1)
    assert [a.kind for a in rec.health(rec.inventory("demo"))] == ["terminate_unknown"]
    assert provider.terminated == ["sbx-ghost"]


def test_orphaned_leases_are_released_and_running_ones_kept(registry):
    rec, provider, _ = _make(registry)
    _vm(registry, provider, "sbx-a")
    _vm(registry, provider, "sbx-b")
    _vm(registry, provider, "sbx-c")
    _claim(registry, "sbx-a", owner="closed")
    _claim(registry, "sbx-b", owner="running")
    _claim(registry, "sbx-c", owner="vanished")
    status = {"closed": "TERMINATED", "running": "RUNNING"}
    actions = rec.leases(rec.inventory("demo"), lambda wf, run: status.get(wf))
    assert sorted(a.vm_id for a in actions) == ["sbx-a", "sbx-c"]
    assert registry.get_vm("sbx-a")["state"] == "recycling"
    assert registry.get_vm("sbx-b")["state"] == "leased"
    assert registry.get_vm("sbx-c")["state"] == "recycling"


def test_expired_lease_with_a_non_running_owner_is_released(registry):
    rec, provider, clock = _make(registry)
    _vm(registry, provider, "sbx-a")
    _claim(registry, "sbx-a", owner="stuck")
    assert rec.leases(rec.inventory("demo"), lambda wf, run: "RUNNING") == []
    clock.advance(61)
    assert rec.leases(rec.inventory("demo"), lambda wf, run: "RUNNING") == []
    assert [a.kind for a in rec.leases(rec.inventory("demo"), lambda wf, run: "UNKNOWN")] == [
        "orphan"
    ]


def test_scale_in_retires_the_oldest_unprotected_idle_vm_after_the_cooldown(registry):
    rec, provider, clock = _make(registry, min_idle=1)
    _vm(registry, provider, "sbx-old")
    _vm(registry, provider, "sbx-mid")
    _vm(registry, provider, "sbx-new")
    assert rec.capacity(rec.inventory("demo")) == [], "cooldown not reached"
    clock.advance(Tunables.local().scale_in_cooldown_seconds + 1)
    actions = rec.capacity(rec.inventory("demo"))
    assert [a.kind for a in actions] == ["scale_in"] and actions[0].vm_id == "sbx-old"
    assert registry.get_vm("sbx-old")["state"] == "terminated"
    assert provider.terminated == ["sbx-old"]
    registry.record_pending("r1", "demo", "wf")
    # Two idle VMs already cover floor 1 plus one pending request, so nothing launches,
    # and a pending request must also block scale-in.
    assert rec.capacity(rec.inventory("demo")) == [], "pending blocks scale-in"


def test_abandoned_requests(registry):
    rec, _, clock = _make(registry)
    registry.record_pending("r1", "demo", "wf")
    assert rec.requests(rec.inventory("demo")) == []
    clock.advance(Tunables.local().abandon_after_seconds + 1)
    assert [a.kind for a in rec.requests(rec.inventory("demo"))] == ["abandon"]
    assert registry.pending_count("demo") == 0


def test_run_records_a_fleet_sample(registry):
    rec, provider, _ = _make(registry)
    _vm(registry, provider, "sbx-a")
    report = rec.run("demo", lambda wf, run: "RUNNING")
    # One VM was launched but not registered, so it counts as booting.
    assert report.counts["idle"] == 1 and report.counts["booting"] == 1
    assert report.counts["min_idle"] == 2 and report.counts["max"] == 5
    assert registry.latest_fleet_sample()["details"]["total"] == 2


def test_tunables_profiles():
    assert Tunables.for_profile("local").stale_after_seconds == 30
    assert Tunables.for_profile("prod").boot_deadline_seconds == 600
    assert Tunables.for_profile("test").scale_in_cooldown_seconds == 2
