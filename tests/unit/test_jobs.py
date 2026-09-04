"""VM agent job store: spawning, observing, cancelling, tailing, and pruning jobs."""

import os
import signal
import subprocess
import time
from unittest.mock import patch

import pytest

from sandbox.contract.errors import Incompatible
from sandbox.contract.types import ExecSpec
from sandbox.vm_agent.jobs import _WRAPPER, JobStore


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


def test_prune_removes_lost_jobs(store, tmp_path):
    store.start(_spec(tmp_path, "j7", ["sleep", "30"]), _env())
    time.sleep(0.2)
    os.killpg(store.pgid("j7"), signal.SIGKILL)
    deadline = time.time() + 5
    while time.time() < deadline and store.status("j7").running:
        time.sleep(0.05)
    assert store.status("j7").lost is True
    time.sleep(0.05)
    assert store.prune(older_than_seconds=0) == 1
    assert store.exists("j7") is False


def test_prune_removes_directories_with_no_meta(store):
    leftover = store.root / "half-started"
    leftover.mkdir()
    assert store.prune(older_than_seconds=0) == 1
    assert leftover.exists() is False


def test_start_recovers_from_a_directory_left_by_a_crashed_start(store, tmp_path):
    d = store.job_dir("j8")
    d.mkdir(parents=True)
    (d / "spec.json").write_text("{}")
    store.start(_spec(tmp_path, "j8", ["sh", "-c", "echo recovered"]), _env())
    _wait_done(store, "j8")
    assert store.read_tail("j8", "stdout") == "recovered\n"


def test_wrap_hands_the_environment_to_sudo_not_to_argv(tmp_path):
    store = JobStore(tmp_path / "jobs", vm_id="sbx-x", run_as_user="agent")
    spec = _spec(tmp_path, "j9", ["claude", "-p", "do the thing"])
    env = {"PATH": "/caller/bin", "HOME": "/home/agent", "GITHUB_TOKEN": "t"}
    d = store.job_dir("j9")
    assert store._wrap(spec, env, d) == [
        "/bin/sh",
        "-c",
        _WRAPPER,
        str(d),
        "/usr/bin/sudo",
        "-n",
        "-u",
        "agent",
        "--preserve-env=GITHUB_TOKEN,HOME,PATH",
        "--",
        *spec.argv,
    ]


def test_the_wrapper_environment_takes_its_path_from_the_agent_not_the_caller(tmp_path):
    # A caller-chosen PATH must not decide which `sh` or `sudo` runs, so the
    # wrapper's own PATH is the agent's however the request was shaped.
    store = JobStore(tmp_path / "jobs", vm_id="sbx-x", run_as_user="agent")
    env = {"PATH": "/caller/bin", "HOME": "/home/agent"}
    with patch.dict(os.environ, {"PATH": "/agent/bin"}):
        assert store._popen_env(env) == {"PATH": "/agent/bin", "HOME": "/home/agent"}
    with patch.dict(os.environ, {}, clear=True):
        assert store._popen_env(env)["PATH"] == (
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )


def test_signal_group_signals_the_group_directly_when_there_is_no_sudo(store):
    with patch("sandbox.vm_agent.jobs._killpg") as killpg:
        store._signal_group(4242, signal.SIGTERM)
    killpg.assert_called_once_with(4242, signal.SIGTERM)


def test_signal_group_also_signals_through_sudo_when_jobs_run_as_another_user(tmp_path):
    # os.killpg from the worker reaches only its own `sh`: sudo runs as root and
    # the command as `agent`, and the worker may signal neither. The same signal
    # therefore goes back through sudo, where the job user can signal its own.
    store = JobStore(tmp_path / "jobs", vm_id="sbx-x", run_as_user="agent")
    with (
        patch("sandbox.vm_agent.jobs._killpg") as killpg,
        patch("sandbox.vm_agent.jobs.subprocess.run") as run,
    ):
        store._signal_group(4242, signal.SIGKILL)
    killpg.assert_called_once_with(4242, signal.SIGKILL)
    assert run.call_args.args[0] == [
        "/usr/bin/sudo",
        "-n",
        "-u",
        "agent",
        "--",
        "kill",
        "-KILL",
        "--",
        "-4242",
    ]
    assert run.call_args.kwargs["check"] is False


def test_a_denied_killpg_still_lets_the_sudo_kill_through(tmp_path):
    # The SIGKILL escalation is reached exactly when the wrapper `sh` is already
    # dead and only the job user's processes are left in the group. Linux
    # answers that killpg with EPERM, not ESRCH, and letting it propagate would
    # abort before the sudo kill that is the only thing able to reach them.
    store = JobStore(tmp_path / "jobs", vm_id="sbx-x", run_as_user="agent")
    with (
        patch("sandbox.vm_agent.jobs.os.killpg", side_effect=PermissionError(1, "not permitted")),
        patch("sandbox.vm_agent.jobs.subprocess.run") as run,
    ):
        store._signal_group(4242, signal.SIGKILL)
    assert run.call_args.args[0] == [
        "/usr/bin/sudo",
        "-n",
        "-u",
        "agent",
        "--",
        "kill",
        "-KILL",
        "--",
        "-4242",
    ]


def test_survivors_asks_pgrep_as_the_job_user_when_jobs_run_as_another_user(tmp_path):
    store = JobStore(tmp_path / "jobs", vm_id="sbx-x", run_as_user="agent")
    argv = ["/usr/bin/sudo", "-n", "-u", "agent", "--", "pgrep", "-g", "4242"]
    with patch("sandbox.vm_agent.jobs.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(argv, 0, "4243\n", "")
        assert store._survivors("j1", 4242) is True
        run.return_value = subprocess.CompletedProcess(argv, 1, "", "")
        assert store._survivors("j1", 4242) is False
    assert run.call_args.args[0] == argv


def test_a_survivor_check_that_cannot_run_is_not_an_all_clear(tmp_path):
    # Only rc 1 is "no match". Reading a sudo denial or a missing pgrep as
    # "nothing left" is how cancel would write exit=-1 over a live job.
    store = JobStore(tmp_path / "jobs", vm_id="sbx-x", run_as_user="agent")
    argv = ["/usr/bin/sudo", "-n", "-u", "agent", "--", "pgrep", "-g", "4242"]
    with patch("sandbox.vm_agent.jobs.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(argv, 2, "", "sudo: a password is required")
        with pytest.raises(RuntimeError) as err:
            store._survivors("j1", 4242)
    assert "job j1: could not check for survivors" in str(err.value)
    assert "pgrep rc=2" in str(err.value) and "password is required" in str(err.value)


def test_cancel_refuses_to_record_an_exit_code_while_the_group_survives(store, tmp_path):
    # The whole point of the sudo kill: without survivors gone, writing exit=-1
    # would tell the orchestrator the job is over while it is still running.
    store.start(_spec(tmp_path, "j10", ["sleep", "30"]), _env())
    time.sleep(0.2)
    pgid = store.pgid("j10")
    with (
        patch.object(JobStore, "_signal_group"),
        patch("sandbox.vm_agent.jobs._KILL_GRACE_SECONDS", 0.2),
        pytest.raises(RuntimeError) as err,
    ):
        store.cancel("j10", grace_seconds=0.1)
    assert f"process group {pgid} still has survivors" in str(err.value)
    assert not (store.job_dir("j10") / "exit").exists()
    os.killpg(pgid, signal.SIGKILL)


def test_a_job_id_that_collides_on_disk_is_refused(store, tmp_path):
    store.start(_spec(tmp_path, "a.b", ["sh", "-c", "echo one"]), _env())
    with pytest.raises(Incompatible) as err:
        store.start(_spec(tmp_path, "a-b", ["sh", "-c", "echo two"]), _env())
    assert err.value.type == "Incompatible"
    st = _wait_done(store, "a.b")
    assert st.exit_code == 0
    assert store.running_job_ids() == []


def test_unknown_streams_are_refused(store):
    with pytest.raises(ValueError):
        store.log_path("j1", "stdlog")
    with pytest.raises(ValueError):
        store.read_tail("j1", "stdlog")
