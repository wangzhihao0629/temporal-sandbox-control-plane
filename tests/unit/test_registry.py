"""Registry: conditional claim and release against moto."""

import time
from datetime import timedelta

import pytest

from sandbox.registry.client import LEASE_FIELDS
from sandbox.registry.schema import STATES
from sandbox.timeutil import epoch_in, now, now_iso, to_iso


def _register(registry, vm_id, pool="demo", labels=None):
    registry.register_vm(
        vm_id=vm_id,
        pool=pool,
        provider_ref=vm_id,
        agent_version="test",
        contract_majors=[1],
        labels=labels or {},
    )
    assert registry.set_state(vm_id, "idle", expect="booting")


def _claim(registry, request_id, pool="demo", labels=None, major=1):
    return registry.claim_idle(
        pool,
        lease_id=f"lease-{request_id}",
        request_id=request_id,
        owner_workflow_id=f"wf-{request_id}",
        owner_run_id="run",
        hold_seconds=60,
        contract_major=major,
        labels=labels or {},
    )


def test_register_and_read_back(registry):
    _register(registry, "sbx-a")
    row = registry.get_vm("sbx-a")
    assert row["state"] == "idle"
    assert row["contract_majors"] == [1]
    assert "idle" in STATES
    assert [r["vm_id"] for r in registry.list_vms("demo")] == ["sbx-a"]


def test_set_state_with_wrong_expectation_is_rejected(registry):
    _register(registry, "sbx-a")
    assert registry.set_state("sbx-a", "leased", expect="booting") is False
    assert registry.get_vm("sbx-a")["state"] == "idle"


def test_claim_takes_the_oldest_idle_vm_only_once(registry):
    _register(registry, "sbx-old")
    time.sleep(0.01)
    _register(registry, "sbx-new")
    first = _claim(registry, "r1")
    second = _claim(registry, "r2")
    third = _claim(registry, "r3")
    assert first["vm_id"] == "sbx-old"
    assert second["vm_id"] == "sbx-new"
    assert third is None
    assert registry.get_vm("sbx-old")["state"] == "leased"
    assert registry.get_vm("sbx-old")["protected"] is True
    assert registry.get_vm("sbx-old")["owner_workflow_id"] == "wf-r1"


def test_re_registering_a_leased_vm_does_not_free_it(registry):
    _register(registry, "sbx-a")
    leased = _claim(registry, "r1")
    registry.register_vm(
        vm_id="sbx-a",
        pool="demo",
        provider_ref="sbx-a",
        agent_version="test-2",
        contract_majors=[1],
        labels={},
    )
    after = registry.get_vm("sbx-a")
    assert after["lease_id"] == leased["lease_id"]
    assert after["protected"] is True
    assert after["agent_version"] == "test-2"
    # `booting` here would be a row nothing can reach: unclaimable because the
    # lease fields are still set, and invisible to find_lease_by_request once
    # the agent's own boot sequence follows the registration with `idle`.
    assert after["state"] == "leased"
    assert registry.find_lease_by_request("r1")["vm_id"] == "sbx-a"
    assert _claim(registry, "r2") is None


def test_re_registering_an_unleased_vm_does_move_it_back_to_booting(registry):
    _register(registry, "sbx-a")
    row = _claim(registry, "r1")
    assert registry.release("sbx-a", row["lease_id"], "recycle") is True
    assert registry.get_vm("sbx-a")["state"] == "recycling"
    registry.register_vm(
        vm_id="sbx-a",
        pool="demo",
        provider_ref="sbx-a",
        agent_version="test-2",
        contract_majors=[1],
        labels={},
    )
    after = registry.get_vm("sbx-a")
    assert after["state"] == "booting" and after["agent_version"] == "test-2"


def test_claim_respects_labels_and_contract_major(registry):
    _register(registry, "sbx-arm", labels={"arch": "arm64"})
    assert _claim(registry, "r1", labels={"arch": "x86_64"}) is None
    assert _claim(registry, "r2", major=2) is None
    assert _claim(registry, "r3", labels={"arch": "arm64"})["vm_id"] == "sbx-arm"


def test_find_lease_by_request_supports_idempotent_acquire(registry):
    _register(registry, "sbx-a")
    row = _claim(registry, "r1")
    found = registry.find_lease_by_request("r1")
    assert found["lease_id"] == row["lease_id"] == "lease-r1"
    assert registry.find_lease_by_request("nope") is None


def test_release_is_idempotent_and_clears_lease_fields(registry):
    _register(registry, "sbx-a")
    row = _claim(registry, "r1")
    assert registry.release("sbx-a", row["lease_id"], "recycle") is True
    assert registry.release("sbx-a", row["lease_id"], "recycle") is False
    after = registry.get_vm("sbx-a")
    assert after["state"] == "recycling"
    assert after["protected"] is False
    assert not any(f in after for f in LEASE_FIELDS)
    assert registry.find_lease_by_request("r1") is None


def test_release_rejects_an_unknown_disposition(registry):
    _register(registry, "sbx-a")
    row = _claim(registry, "r1")
    with pytest.raises(ValueError):
        registry.release("sbx-a", row["lease_id"], "obliterate")


def test_heartbeat_returns_state_and_none_when_row_is_gone(registry):
    _register(registry, "sbx-a")
    assert registry.heartbeat("sbx-a") == "idle"
    registry.delete_vm("sbx-a")
    assert registry.heartbeat("sbx-a") is None


def test_touch_lease_extends_expiry(registry):
    _register(registry, "sbx-a")
    row = _claim(registry, "r1")
    time.sleep(0.01)
    registry.touch_lease("sbx-a")
    assert registry.get_vm("sbx-a")["lease_expires_at"] > row["lease_expires_at"]


def test_jobs_events_and_requests(registry):
    registry.put_job(
        "sbx-a", "j1", status="running", argv_summary="echo hi", owner_workflow_id="wf"
    )
    registry.update_job("sbx-a", "j1", status="exited", exit_code=0)
    jobs = registry.list_jobs("sbx-a")
    assert jobs[0]["job_id"] == "j1" and jobs[0]["status"] == "exited" and jobs[0]["exit_code"] == 0

    registry.emit("acquire", "manager", "leased sbx-a", vm_id="sbx-a", details={"lease": "l1"})
    events = registry.recent_events(limit=10)
    assert events[0]["type"] == "acquire" and events[0]["details"] == {"lease": "l1"}

    registry.record_pending("r9", "demo", "wf-9")
    assert registry.pending_count("demo") == 1
    registry.fulfill_request("r9")
    assert registry.pending_count("demo") == 0


def test_job_floats_round_trip_as_floats(registry):
    """DynamoDB has no float type, so the client coerces them on the way in."""
    registry.put_job("sbx-a", "j2", status="running", duration_seconds=0.0)
    registry.update_job("sbx-a", "j2", status="exited", duration_seconds=2.125)
    registry.emit("exec", "vm-agent", "job done", vm_id="sbx-a", details={"seconds": 2.125})

    job = registry.list_jobs("sbx-a")[0]
    assert job["duration_seconds"] == 2.125 and isinstance(job["duration_seconds"], float)
    assert registry.recent_events(limit=1)[0]["details"] == {"seconds": 2.125}


def test_record_pending_keeps_the_age_of_the_first_attempt(registry):
    # The client re-asks with the same request_id every few seconds, so a queue
    # of starving requests must not look brand new on every retry.
    registry.record_pending("r9", "demo", "wf-9")
    first = registry.requests.get_item(Key={"request_id": "r9"})["Item"]
    time.sleep(0.01)
    registry.record_pending("r9", "demo", "wf-9")
    again = registry.requests.get_item(Key={"request_id": "r9"})["Item"]
    assert again["created_at"] == first["created_at"]
    assert registry.pending_count("demo") == 1


def test_recent_events_spills_into_yesterdays_partition(registry):
    # The partition key is the UTC date, so just after midnight today's
    # partition is nearly empty and a dashboard reading only it shows nothing.
    yesterday = to_iso(now() - timedelta(days=1))
    registry.events.put_item(
        Item={
            "day": yesterday[:10],
            "ts_ulid": f"{yesterday}#old00000",
            "type": "boot",
            "actor": "vm-agent",
            "vm_id": "sbx-a",
            "message": "yesterday",
            "details": {},
            "ttl": epoch_in(3600),
        }
    )
    registry.emit("acquire", "manager", "today", vm_id="sbx-a")
    messages = [e["message"] for e in registry.recent_events(limit=10)]
    assert messages == ["today", "yesterday"]
    assert [e["message"] for e in registry.recent_events(limit=1)] == ["today"]
    assert now_iso() > yesterday
