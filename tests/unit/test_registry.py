"""Registry: conditional claim and release against moto."""

import time

from sandbox.registry.schema import STATES


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
    # created_at comes from now_iso(), which is second-resolution, so the two rows
    # need to land in different seconds for "oldest" to be a decidable question.
    time.sleep(1.1)
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
    assert "lease_id" not in after
    assert registry.find_lease_by_request("r1") is None


def test_heartbeat_returns_state_and_none_when_row_is_gone(registry):
    _register(registry, "sbx-a")
    assert registry.heartbeat("sbx-a") == "idle"
    registry.delete_vm("sbx-a")
    assert registry.heartbeat("sbx-a") is None


def test_touch_lease_extends_expiry(registry):
    _register(registry, "sbx-a")
    row = _claim(registry, "r1")
    registry.touch_lease("sbx-a")
    assert registry.get_vm("sbx-a")["lease_expires_at"] >= row["lease_expires_at"]


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
