"""Step specs name jobs predictably, call the launcher, and carry the test hooks."""

from sandbox.orchestrator import steps
from sandbox.orchestrator.steps import SessionUris, StepContext

CTX = StepContext(
    runner_path="/var/lib/sandbox/artifacts/abc",
    workspace="/private/tmp/sandbox/session-1",
    session_id="s1",
    attempt=2,
    env={"RUNNER_PYTHON": "/usr/bin/python3"},
)
URIS = SessionUris("s1")


def test_uris_follow_the_layout():
    assert URIS.session == "s3://sandbox-sessions/s1"
    assert URIS.envelope("j") == "s3://sandbox-sessions/s1/steps/j.json"
    assert URIS.log("j") == "s3://sandbox-jobs/s1/j"
    assert URIS.patch == "s3://sandbox-out/s1/session.patch"
    assert URIS.summary == "s3://sandbox-out/s1/summary.json"


def test_job_ids_encode_step_turn_and_attempt():
    assert steps.job_id(CTX, "clone", 0) == "s1-clone-t0-a2"
    assert steps.job_id(CTX, "test", 3) == "s1-test-t3-a2"


def test_clone_spec_calls_the_launcher_in_the_workspace():
    spec = steps.clone_spec(CTX, URIS, "hello")
    assert spec.job_id == "s1-clone-t0-a2"
    assert spec.argv[:3] == ["/bin/sh", "/var/lib/sandbox/artifacts/abc/bin/runner", "clone"]
    assert "--repo" in spec.argv and spec.argv[spec.argv.index("--repo") + 1] == "hello"
    assert spec.argv[spec.argv.index("--envelope-uri") + 1] == URIS.envelope(spec.job_id)
    assert spec.argv[spec.argv.index("--session-uri") + 1] == URIS.session
    assert spec.cwd == CTX.workspace and spec.env == CTX.env
    assert spec.log_uri == URIS.log(spec.job_id)
    assert spec.timeout_seconds == steps.STEP_TIMEOUT_SECONDS


def test_turn_spec_passes_feedback_or_none_and_pads_the_timeout():
    spec = steps.turn_spec(CTX, URIS, 2, "add multiply", "", "", 30, "fake")
    argv = spec.argv
    assert argv[2] == "turn" and argv[argv.index("--feedback-uri") + 1] == "none"
    assert argv[argv.index("--scenario") + 1] == "" and argv[argv.index("--agent") + 1] == "fake"
    assert argv[argv.index("--seconds") + 1] == "30"
    assert spec.timeout_seconds == 30 + steps.TURN_TIMEOUT_SLACK_SECONDS
    with_feedback = steps.turn_spec(CTX, URIS, 2, "p", "never-fixes", "s3://f", 0, "fake")
    assert with_feedback.argv[with_feedback.argv.index("--feedback-uri") + 1] == "s3://f"


def test_check_and_export_specs():
    assert steps.lint_spec(CTX, URIS, 1).argv[2] == "lint"
    assert steps.test_spec(CTX, URIS, 1).job_id == "s1-test-t1-a2"
    export = steps.export_spec(CTX, URIS, 2)
    assert export.argv[2] == "export" and "--session-uri" in export.argv
