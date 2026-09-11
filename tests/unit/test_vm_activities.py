"""VM activity helpers: heartbeat cadence and the environment a job actually gets."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from temporalio.exceptions import ApplicationError

from sandbox.contract.types import ExecSpec
from sandbox.vm_agent.activities import _HEARTBEAT_EVERY, VmActivities, _heartbeat_interval
from sandbox.vm_agent.config import AgentConfig
from sandbox.vm_agent.drain import DrainState
from sandbox.vm_agent.jobs import JobStore


@pytest.fixture
def activities(tmp_path):
    cfg = AgentConfig(
        vm_id="sbx-test",
        pool="demo",
        provider_ref="sbx-test",
        temporal_address="",
        temporal_namespace="default",
        agent_version="test",
        jobs_dir=tmp_path / "jobs",
        artifacts_dir=tmp_path / "artifacts",
        workspace_root=tmp_path / "ws",
        secrets_dir=tmp_path / "secrets",
        run_as_user=None,
        heartbeat_seconds=1.0,
    )
    cfg.workspace_root.mkdir()
    cfg.secrets_dir.mkdir()
    jobs = JobStore(cfg.jobs_dir, cfg.vm_id)
    return VmActivities(cfg, jobs, registry=None, store=None, drain=DrainState())


def _spec(activities, **kw):
    return ExecSpec(job_id="j1", argv=["true"], cwd=str(activities.cfg.workspace_root), **kw)


def test_heartbeat_interval_stays_under_the_timeout_temporal_will_enforce():
    # A fixed 5 s cadence under a 6 s heartbeat timeout leaves no room for a
    # slow log upload; the cadence has to shrink with the timeout.
    assert _heartbeat_interval(None) == _HEARTBEAT_EVERY
    assert _heartbeat_interval(timedelta(minutes=2)) == _HEARTBEAT_EVERY
    assert _heartbeat_interval(timedelta(seconds=6)) == 2.0
    assert _heartbeat_interval(timedelta(seconds=3)) == 1.0


def test_a_caller_cannot_point_the_vm_at_its_own_object_store(activities):
    # S3_ENDPOINT is not credential-shaped, so validate_env lets it through.
    # It must still never reach the job: the VM uploads logs and artifacts with
    # its own identity, and a caller that could redirect that would be told
    # where the VM's own credentials get sent.
    spec = _spec(activities, env={"S3_ENDPOINT": "http://attacker.example", "PROMPT": "hi"})
    with patch.dict("os.environ", {"S3_ENDPOINT": "http://127.0.0.1:5050"}):
        env = activities._job_env(spec)
    assert env["S3_ENDPOINT"] == "http://127.0.0.1:5050"
    assert env["PROMPT"] == "hi"


def test_a_caller_supplied_identity_key_is_dropped_even_when_the_agent_has_none(activities):
    # Conditional injection alone would leave the caller's value standing here.
    spec = _spec(activities, env={"S3_ENDPOINT": "http://attacker.example"})
    with patch.dict("os.environ", {}, clear=True):
        env = activities._job_env(spec)
    assert "S3_ENDPOINT" not in env


def test_credential_shaped_identity_keys_are_refused_at_the_boundary(activities):
    with pytest.raises(ApplicationError) as err:
        activities._job_env(_spec(activities, env={"AWS_SECRET_ACCESS_KEY": "x"}))
    assert err.value.type == "Incompatible"


class _RecordingStore:
    def __init__(self):
        self.uploads: list[str] = []

    def upload_file(self, path, uri):
        self.uploads.append(uri)
        return 0


def test_log_sync_uploads_only_grown_logs_and_not_more_often_than_the_cadence(
    activities, monkeypatch
):
    # A job that runs for hours must not re-send its whole log every heartbeat.
    activities.store = _RecordingStore()
    log = activities.jobs.log_path("j1", "stdout")
    log.parent.mkdir(parents=True)
    log.write_text("line 1\n")
    uri = "s3://sandbox-jobs/s/j1"

    sizes = activities._sync_logs("j1", uri)
    assert sizes["stdout"] == 7 and activities.store.uploads == [f"{uri}/stdout.log"]

    activities._sync_logs("j1", uri)  # nothing new: no upload
    log.write_text("line 1\nline 2\n")
    activities._sync_logs("j1", uri)  # grown, but the cadence has not elapsed
    assert len(activities.store.uploads) == 1

    monkeypatch.setattr("sandbox.vm_agent.activities._LOG_SYNC_EVERY", 0.0)
    activities._sync_logs("j1", uri)  # grown and due
    assert len(activities.store.uploads) == 2

    monkeypatch.setattr("sandbox.vm_agent.activities._LOG_SYNC_EVERY", 1000.0)
    activities._sync_logs("j1", uri, force=True)  # the final flush ignores the cadence
    assert len(activities.store.uploads) == 3 and "j1" not in activities._log_sync
    assert activities._sync_logs("j1", "")["stdout"] == 14, "no uri: sizes only, no upload"
    assert len(activities.store.uploads) == 3
