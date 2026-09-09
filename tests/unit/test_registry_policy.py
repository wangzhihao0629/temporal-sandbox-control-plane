"""Registry: pool policy items, pending requests, write-off, TTL, pagination guards."""

import boto3
import pytest

from sandbox.manager.policy import PoolPolicy
from sandbox.registry.schema import EVENTS_TABLE, REQUESTS_TABLE, VMS_TABLE, create_tables


def _idle(registry, vm_id, pool="demo"):
    registry.register_vm(vm_id, pool, vm_id, "test", [1], {})
    assert registry.set_state(vm_id, "idle", expect="booting")


def test_policy_round_trip_and_defaults(registry):
    assert registry.get_policy("demo") is None
    row = registry.ensure_policy("demo", min_idle=2, max=5, image="sandbox-vm:dev")
    assert PoolPolicy.from_row(row) == PoolPolicy("demo", 2, 5, "sandbox-vm:dev", 2, "2048M")
    again = registry.ensure_policy("demo", min_idle=9, max=9, image="other")
    assert again["min_idle"] == 2, "ensure_policy must not overwrite an existing item"
    updated = registry.put_policy("demo", min_idle=3, max=6, image="sandbox-vm:dev")
    assert registry.get_policy("demo")["min_idle"] == 3 and updated["max"] == 6
    assert [p["pool"] for p in registry.list_pools()] == ["demo"]


def test_policy_items_never_look_like_vms(registry):
    registry.ensure_policy("demo", min_idle=2, max=5, image="img")
    _idle(registry, "sbx-a")
    assert [r["vm_id"] for r in registry.list_vms()] == ["sbx-a"]
    row = registry.claim_idle(
        "demo",
        lease_id="l",
        request_id="r",
        owner_workflow_id="wf",
        owner_run_id="run",
        hold_seconds=60,
        contract_major=1,
        labels={},
    )
    assert row["vm_id"] == "sbx-a"
    assert (
        registry.claim_idle(
            "demo",
            lease_id="l2",
            request_id="r2",
            owner_workflow_id="wf",
            owner_run_id="run",
            hold_seconds=60,
            contract_major=1,
            labels={},
        )
        is None
    )


def test_pending_requests_listing_and_abandon(registry):
    registry.record_pending("r1", "demo", "wf-1")
    registry.record_pending("r2", "demo", "wf-2")
    registry.record_pending("r3", "other", "wf-3")
    assert sorted(r["request_id"] for r in registry.pending_requests("demo")) == ["r1", "r2"]
    assert registry.abandon_request("r1") is True
    assert registry.abandon_request("r1") is False
    assert [r["request_id"] for r in registry.pending_requests("demo")] == ["r2"]
    assert registry.pending_count("demo") == 1


def test_pending_requests_paginates(registry):
    for i in range(40):
        registry.record_pending(f"r{i:03d}", "demo", "x" * 30000)
    assert len(registry.pending_requests("demo")) == 40
    assert registry.pending_count("demo") == 40


def test_write_off_removes_the_lease_so_the_old_owner_cannot_release(registry):
    _idle(registry, "sbx-a")
    row = registry.claim_idle(
        "demo",
        lease_id="l",
        request_id="r",
        owner_workflow_id="wf",
        owner_run_id="run",
        hold_seconds=60,
        contract_major=1,
        labels={},
    )
    assert registry.write_off("sbx-a", "instance missing") is True
    after = registry.get_vm("sbx-a")
    assert after["state"] == "terminated" and "lease_id" not in after
    assert after["protected"] is False
    assert "ttl" in after and after["reason"] == "instance missing"
    assert registry.release("sbx-a", row["lease_id"], "recycle") is False
    assert registry.write_off("sbx-missing", "x") is False
    # Writing the same corpse off again must not push its sweep deadline out.
    assert registry.write_off("sbx-a", "instance stopped") is False
    assert registry.get_vm("sbx-a")["reason"] == "instance missing"
    assert registry.get_vm("sbx-a")["last_transition_at"] == after["last_transition_at"]


def test_set_state_rejects_unknown_states(registry):
    _idle(registry, "sbx-a")
    with pytest.raises(ValueError):
        registry.set_state("sbx-a", "sleeping")


def test_ttl_is_enabled_on_the_tables_that_carry_it(registry):
    # create_tables must be idempotent: calling it again against existing
    # tables, with TTL already enabled, must not raise.
    create_tables(boto3.resource("dynamodb", region_name="us-east-1"))
    client = registry.vms.meta.client
    for table in (VMS_TABLE, EVENTS_TABLE, REQUESTS_TABLE):
        status = client.describe_time_to_live(TableName=table)["TimeToLiveDescription"]
        assert status["TimeToLiveStatus"] == "ENABLED" and status["AttributeName"] == "ttl"


def test_latest_fleet_sample(registry):
    assert registry.latest_fleet_sample() is None
    registry.emit("fleet_sample", "reconciler", "sample", details={"total": 1, "idle": 1})
    registry.emit("acquire", "manager", "noise", vm_id="sbx-a")
    sample = registry.latest_fleet_sample()
    assert sample["details"] == {"total": 1, "idle": 1}
