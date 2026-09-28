"""Registry rows, provider instances, summaries, and events become the dashboard's JSON."""

from datetime import UTC, datetime, timedelta

from sandbox.status import joins

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _row(**over):
    base = {
        "vm_id": "sbx-a",
        "pool": "demo",
        "state": "idle",
        "provider_ref": "sbx-a",
        "agent_version": "0.1.0",
        "protected": False,
        "created_at": _iso(NOW - timedelta(minutes=10)),
        "last_heartbeat_at": _iso(NOW - timedelta(seconds=7)),
        "last_transition_at": _iso(NOW - timedelta(minutes=1)),
        "reason": "ready",
    }
    base.update(over)
    return base


def test_heartbeat_age_rounds_down_to_seconds():
    assert joins.heartbeat_age(_row(), NOW) == 7


def test_vm_view_joins_instance_job_and_owner():
    row = _row(
        state="leased",
        lease_id="L1",
        owner_workflow_id="session-x",
        owner_run_id="r1",
        lease_started_at=_iso(NOW - timedelta(seconds=90)),
        lease_expires_at=_iso(NOW + timedelta(minutes=28)),
    )
    instance = {"provider_ref": "sbx-a", "state": "running", "address": "192.168.64.5"}
    job = {"job_id": "s-turn-t1-a1", "argv_summary": "/bin/sh runner turn", "started_at": "t"}
    view = joins.vm_view(row, instance, job, "RUNNING", NOW)
    assert view["vm_id"] == "sbx-a" and view["state"] == "leased"
    assert view["heartbeat_age_seconds"] == 7 and view["provider_state"] == "running"
    assert view["address"] == "192.168.64.5"
    assert view["owner_workflow_id"] == "session-x" and view["owner_status"] == "RUNNING"
    assert view["lease_age_seconds"] == 90
    assert view["current_job"] == {
        "job_id": "s-turn-t1-a1",
        "argv_summary": "/bin/sh runner turn",
        "started_at": "t",
    }


def test_vm_view_without_instance_or_job_or_owner():
    view = joins.vm_view(_row(), None, None, None, NOW)
    assert view["provider_state"] == "missing" and view["address"] == ""
    assert view["current_job"] is None and view["owner_status"] is None
    assert view["lease_age_seconds"] is None and view["lease_id"] == ""


def test_lease_view_reports_age_and_owner():
    row = _row(
        state="leased",
        lease_id="L1",
        lease_request_id="req-1",
        owner_workflow_id="session-x",
        owner_run_id="r1",
        lease_started_at=_iso(NOW - timedelta(seconds=30)),
        lease_expires_at="later",
    )
    view = joins.lease_view(row, "TERMINATED", NOW)
    assert view == {
        "vm_id": "sbx-a",
        "lease_id": "L1",
        "request_id": "req-1",
        "owner_workflow_id": "session-x",
        "owner_run_id": "r1",
        "owner_status": "TERMINATED",
        "age_seconds": 30,
        "expires_at": "later",
    }


def test_fleet_view_prefers_the_reconciler_sample():
    sample = {
        "ts_ulid": "2026-09-10T11:59:58.000Z#abcd",
        "details": {
            "total": 3,
            "idle": 1,
            "leased": 2,
            "booting": 0,
            "recycling": 0,
            "draining": 0,
            "dead": 4,
            "pending": 1,
            "min_idle": 2,
            "max": 5,
        },
    }
    policy = {"pool": "demo", "min_idle": 2, "max": 5, "image": "sandbox-vm:dev"}
    view = joins.fleet_view(sample, [], 0, policy)
    assert view["source"] == "sample" and view["sample_at"] == "2026-09-10T11:59:58.000Z"
    assert view["counts"]["leased"] == 2 and view["counts"]["dead"] == 4
    assert view["policy"] == policy


def test_fleet_view_counts_rows_when_there_is_no_sample():
    rows = [
        _row(vm_id="a", state="idle"),
        _row(vm_id="b", state="leased"),
        _row(vm_id="c", state="booting"),
        _row(vm_id="d", state="terminated"),
        _row(vm_id="e", state="draining"),
    ]
    view = joins.fleet_view(None, rows, 2, {"pool": "demo", "min_idle": 2, "max": 5})
    assert view["source"] == "rows" and view["sample_at"] is None
    assert view["counts"] == {
        "total": 4,
        "idle": 1,
        "leased": 1,
        "booting": 1,
        "recycling": 0,
        "draining": 1,
        "dead": 1,
        "pending": 2,
        "min_idle": 2,
        "max": 5,
    }


def test_job_view_keeps_the_columns_the_page_shows():
    row = {
        "vm_id": "sbx-a",
        "job_id": "j1",
        "status": "exited",
        "exit_code": 0,
        "argv_summary": "echo hi",
        "started_at": "s",
        "ended_at": "e",
        "duration_seconds": 1.5,
        "log_uri": "s3://sandbox-jobs/x/j1",
        "owner_workflow_id": "wf",
        "updated_at": "u",
        "extra": "dropped",
    }
    view = joins.job_view(row)
    assert "extra" not in view and view["duration_seconds"] == 1.5 and view["exit_code"] == 0
    assert joins.job_view({"vm_id": "a", "job_id": "b"})["status"] == ""


def test_session_view_fills_defaults_for_old_summaries():
    view = joins.session_view({"session_id": "s1", "turns": 2, "tests_passed": True})
    assert view["session_id"] == "s1" and view["workflow_id"] == "session-s1"
    assert view["tests_failed"] == 0 and view["lint_count"] == 0 and view["vm_ids"] == []
    explicit = joins.session_view({"session_id": "s2", "workflow_id": "custom"})
    assert explicit["workflow_id"] == "custom"


def test_sample_series_keeps_the_window_oldest_first():
    def ev(minutes_ago, idle):
        ts = _iso(NOW - timedelta(minutes=minutes_ago))
        return {
            "ts_ulid": f"{ts}#x",
            "type": "fleet_sample",
            "details": {"idle": idle, "leased": 1, "pending": 0, "total": idle + 1},
        }

    events = [ev(1, 2), {"ts_ulid": "z", "type": "launch"}, ev(20, 5), ev(5, 1)]
    series = joins.sample_series(events, 15, NOW)
    assert [p["idle"] for p in series] == [1, 2]
    assert series[0]["ts"] == _iso(NOW - timedelta(minutes=5))
    assert series[0]["total"] == 2 and series[1]["leased"] == 1


def test_tail_text_returns_the_last_bytes_decoded():
    assert joins.tail_text(b"hello\nworld\n", 6) == "world\n"
    assert joins.tail_text(b"short", 100) == "short"
    assert joins.tail_text(b"\xff\xfeok", 10).endswith("ok")


def test_temporal_link():
    assert (
        joins.temporal_link("http://localhost:8233", "default", "wf-1")
        == "http://localhost:8233/namespaces/default/workflows/wf-1"
    )
    assert joins.temporal_link("http://ui/", "default", "wf-1", "r1").endswith("/wf-1/r1")


RUNNER = "/bin/sh /var/lib/sandbox/artifacts/" + "a" * 64 + "/bin/runner"


def _job(job_id, step, status="exited", started="2026-09-28T04:00:00Z", exit_code=0):
    return {
        "job_id": job_id,
        "vm_id": "sbx-a",
        "status": status,
        "exit_code": exit_code,
        "started_at": started,
        "argv_summary": f"{RUNNER} {step} --workspace /ws --envelope-uri s3://b/{job_id}.json",
    }


def test_a_step_is_parsed_from_its_command_and_job_id():
    step = joins.step_view(_job("s1-test-t2-a1", "test"))
    assert (step["step"], step["turn"], step["attempt"]) == ("test", 2, 1)
    assert step["command"] == "runner test --workspace /ws"


def test_a_failing_test_step_is_bad_even_though_it_exited_zero():
    envelope = {"ok": True, "passed": 2, "total": 3, "failures": [{"test": "t::test_multiply"}]}
    assert joins.step_result("test", envelope) == {
        "text": "2/3 passed — t::test_multiply",
        "level": "bad",
    }
    assert joins.step_result("test", {"ok": True, "passed": 3, "total": 3})["level"] == "ok"


def test_step_results_read_like_what_the_step_did():
    assert joins.step_result("run", {"ok": True, "exit_code": 0, "stdout": "Hello\nmore"}) == {
        "text": "→ Hello",
        "level": "ok",
    }
    assert joins.step_result("lint", {"ok": True, "count": 0, "findings": []})["text"] == (
        "no findings"
    )
    assert joins.step_result("build", {"ok": False, "error": "go build failed"}) == {
        "text": "go build failed",
        "level": "bad",
    }


def test_runs_group_steps_by_workflow_in_order_and_carry_the_worst_level():
    first = joins.step_view(_job("s1-clone-t0-a1", "clone", started="2026-09-28T04:00:01Z"))
    second = joins.step_view(_job("s1-test-t1-a1", "test", started="2026-09-28T04:00:05Z"))
    second["result"] = {"text": "2/3 passed", "level": "bad"}
    other = joins.step_view(_job("g1-run-t0-a1", "run", status="running"))
    runs = joins.run_views({"session-s1": [second, first], "gobuild-g1": [other]}, lambda w: w)
    by_id = {r["workflow_id"]: r for r in runs}
    assert [s["step"] for s in by_id["session-s1"]["steps"]] == ["clone", "test"]
    assert by_id["session-s1"]["level"] == "bad" and by_id["session-s1"]["kind"] == "session"
    assert by_id["gobuild-g1"]["status"] == "running"
