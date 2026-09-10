"""JUnit and ruff output become bounded envelopes; a broken tool is ok=False."""

import sys

from sandbox.runner import envelopes
from sandbox.runner.checks import parse_junit, run_pytest, run_ruff

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="4">
<testcase classname="tests.test_calc" name="test_add"/>
<testcase classname="tests.test_calc" name="test_multiply">
  <failure message="assert 5 == 6">Traceback</failure></testcase>
<testcase classname="tests.test_calc" name="test_skip"><skipped/></testcase>
<testcase classname="tests.test_calc" name="test_err"><error message="boom"/></testcase>
</testsuite></testsuites>"""


def test_parse_junit_counts_and_names_failures():
    report = parse_junit(JUNIT)
    assert report.ok and report.total == 4 and report.passed == 1 and report.failed == 2
    assert report.failures[0].test == "tests.test_calc::test_multiply"
    assert report.failures[0].message == "assert 5 == 6"
    assert report.failures[1].test == "tests.test_calc::test_err"
    assert report.kind == "test"


def test_parse_junit_caps_failures_and_messages():
    cases = "".join(
        f'<testcase classname="t" name="n{i}"><failure message="{"x" * 900}"/></testcase>'
        for i in range(30)
    )
    report = parse_junit(f'<testsuite tests="30">{cases}</testsuite>')
    assert report.failed == 30 and len(report.failures) == envelopes.MAX_FAILURES
    assert len(report.failures[0].message) == envelopes.MAX_MESSAGE


def test_pytest_and_ruff_on_a_broken_workspace_report_not_ok(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.ruff]\nline-length = "wide"\n')
    lint = run_ruff(tmp_path, sys.executable)
    assert not lint.ok and lint.error
    (tmp_path / "conftest.py").write_text("raise RuntimeError('broken conftest')\n")
    report = run_pytest(tmp_path, sys.executable)
    assert not report.ok and "broken conftest" in report.error


def test_broken_builds_the_right_envelope():
    env = envelopes.broken("lint", "ruff exploded")
    assert isinstance(env, envelopes.LintEnvelope) and not env.ok and env.error == "ruff exploded"
    assert envelopes.truncate("y" * 600).endswith("…") and len(envelopes.truncate("y" * 600)) == 500
