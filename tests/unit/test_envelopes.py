"""describe(): a step's result, as the lines it adds to the job's own log."""

from sandbox.runner import envelopes as e


def test_a_failing_test_run_names_the_test_and_keeps_the_whole_message():
    env = e.TestEnvelope(
        ok=True,
        passed=2,
        failed=1,
        total=3,
        failures=[
            e.TestFailure(test="tests.test_calc::test_multiply", message="assert 5 == 6\n + where")
        ],
    )
    lines = e.describe(env)
    assert "  failed: 1" in lines
    assert "    - test: tests.test_calc::test_multiply" in lines
    assert "      message: assert 5 == 6" in lines
    assert any("+ where" in line for line in lines), "later message lines are kept, indented"


def test_empty_fields_and_the_bookkeeping_ones_are_left_out():
    lines = e.describe(e.RunEnvelope(ok=True, exit_code=0, stdout="Hello"))
    assert lines == ["  exit_code: 0", "  stdout: Hello"]


def test_a_broken_step_shows_its_error():
    assert e.describe(e.broken("build", "go build failed")) == ["  error: go build failed"]
