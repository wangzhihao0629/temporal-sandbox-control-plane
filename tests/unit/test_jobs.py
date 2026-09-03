"""VM agent job store: spawning, observing, cancelling, tailing, and pruning jobs."""

import os
import signal
import time

import pytest

from sandbox.contract.types import ExecSpec
from sandbox.vm_agent.jobs import JobStore


@pytest.fixture
def store(tmp_path):
    (tmp_path / "ws").mkdir()
    return JobStore(tmp_path / "jobs", vm_id="sbx-test")


def _spec(tmp_path, job_id, argv, timeout=30):
    return ExecSpec(job_id=job_id, argv=argv, cwd=str(tmp_path / "ws"), timeout_seconds=timeout)


def _env():
    return {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}


def _wait_done(store, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = store.status(job_id)
        if not st.running:
            return st
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_start_runs_command_and_captures_exit_and_output(store, tmp_path):
    spec = _spec(tmp_path, "j1", ["sh", "-c", "echo hello; echo oops >&2; exit 3"])
    job = store.start(spec, _env())
    assert job.job_id == "j1" and job.vm_id == "sbx-test"
    st = _wait_done(store, "j1")
    assert st.exit_code == 3 and st.lost is False and st.reason == ""
    assert store.read_tail("j1", "stdout") == "hello\n"
    assert store.read_tail("j1", "stderr") == "oops\n"
    assert store.load_spec("j1").argv[0] == "sh"


def test_start_is_idempotent_on_job_id(store, tmp_path):
    spec = _spec(tmp_path, "j2", ["sh", "-c", "echo once"])
    first = store.start(spec, _env())
    second = store.start(spec, _env())
    assert first == second
    _wait_done(store, "j2")
    assert store.read_tail("j2", "stdout") == "once\n"


def test_cancel_kills_the_whole_process_group(store, tmp_path):
    store.start(_spec(tmp_path, "j3", ["sh", "-c", "sleep 30; echo done"]), _env())
    time.sleep(0.2)
    assert store.status("j3").running is True
    assert "j3" in store.running_job_ids()
    pgid = store.pgid("j3")
    store.cancel("j3", grace_seconds=1)
    st = store.status("j3")
    assert st.running is False and st.reason == "cancelled"
    assert store.read_tail("j3", "stdout") == ""
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_lost_when_the_process_dies_without_writing_exit(store, tmp_path):
    store.start(_spec(tmp_path, "j4", ["sleep", "30"]), _env())
    time.sleep(0.2)
    os.killpg(store.pgid("j4"), signal.SIGKILL)
    deadline = time.time() + 5
    while time.time() < deadline and store.status("j4").running:
        time.sleep(0.05)
    st = store.status("j4")
    assert st.running is False and st.lost is True and st.exit_code is None


def test_a_fresh_store_over_the_same_directory_reattaches(store, tmp_path):
    store.start(_spec(tmp_path, "j5", ["sh", "-c", "sleep 0.3; echo late"]), _env())
    other = JobStore(store.root, vm_id="sbx-test")
    assert other.status("j5").running is True
    st = _wait_done(other, "j5")
    assert st.exit_code == 0
    assert other.read_tail("j5", "stdout") == "late\n"


def test_unknown_job_reports_lost(store):
    st = store.status("never-started")
    assert st.running is False and st.lost is True


def test_prune_removes_finished_old_jobs(store, tmp_path):
    store.start(_spec(tmp_path, "j6", ["true"]), _env())
    _wait_done(store, "j6")
    store.prune(older_than_seconds=0)
    assert store.exists("j6") is False
