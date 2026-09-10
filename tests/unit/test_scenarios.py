"""Every scenario turn applies to a fresh seed and produces the test and lint outcome it claims."""

import io
import shutil
import sys
from pathlib import Path

import pytest

from sandbox.runner import agent
from sandbox.runner.checks import run_pytest, run_ruff
from sandbox.runner.scenarios import (
    DEFAULT_SCENARIO,
    SCENARIOS,
    Scenario,
    Step,
    Turn,
    pick_scenario,
)

SEED = Path(__file__).resolve().parents[2] / "images" / "vm" / "seed" / "hello"


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    shutil.copytree(SEED, ws)
    return ws


def test_seed_is_green_and_clean(workspace):
    report = run_pytest(workspace, sys.executable)
    assert report.ok and report.failed == 0 and report.total == 2
    assert run_ruff(workspace, sys.executable).count == 0


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_each_turn_matches_its_claims(workspace, name):
    scenario = SCENARIOS[name]
    # One turn past the script's end proves the last turn repeats safely.
    for n in range(1, len(scenario.turns) + 2):
        out = io.StringIO()
        outcome = agent.run_fake_turn(workspace, scenario, n, 0, None, out=out)
        assert outcome.ok, outcome.error
        turn = scenario.turn_for(n)
        assert outcome.summary == turn.summary
        assert outcome.fake_cost_usd > 0
        report = run_pytest(workspace, sys.executable)
        assert report.ok
        assert (report.failed == 0) is turn.tests_pass, (n, report)
        assert run_ruff(workspace, sys.executable).count == turn.lint_findings, n
        assert "[tool]" in out.getvalue()


def test_never_fixes_keeps_the_bug_across_repeated_turns(workspace):
    scenario = SCENARIOS["never-fixes"]
    for n in (1, 2, 3):
        assert agent.run_fake_turn(workspace, scenario, n, 0, None, out=io.StringIO()).ok
    assert run_pytest(workspace, sys.executable).failed == 1
    assert "# turn 3:" in (workspace / "calc" / "__init__.py").read_text()


def test_feedback_is_quoted_before_the_fix(workspace):
    scenario = SCENARIOS["multiply-with-bug"]
    agent.run_fake_turn(workspace, scenario, 1, 0, None, out=io.StringIO())
    report = run_pytest(workspace, sys.executable)
    out = io.StringIO()
    outcome = agent.run_fake_turn(workspace, scenario, 2, 0, report.to_dict(), out=out)
    assert outcome.ok
    text = out.getvalue()
    assert "[feedback] 1 failing test" in text
    assert "test_multiply" in text
    assert run_pytest(workspace, sys.executable).failed == 0


def test_apply_edit_reports_a_noop_when_already_applied(tmp_path):
    path = tmp_path / "f.py"
    path.write_text("a = 1\n")
    assert agent.apply_edit(path, "a = 1\n", "a = 2\nb = 3\n") == (2, 1)
    assert agent.apply_edit(path, "a = 1\n", "a = 2\nb = 3\n") is None
    with pytest.raises(LookupError):
        agent.apply_edit(path, "missing", "x")


def test_pick_scenario_by_name_then_keyword_then_default():
    assert pick_scenario("divide-clean", "").name == "divide-clean"
    assert pick_scenario("", "please add divide").name == "divide-clean"
    assert pick_scenario("", "never fix multiply").name == "never-fixes"
    assert pick_scenario("", "add power").name == "lint-only"
    assert pick_scenario("", "hello").name == DEFAULT_SCENARIO
    with pytest.raises(KeyError):
        pick_scenario("nope", "")


def test_claude_turn_without_the_cli_is_a_broken_turn(workspace, monkeypatch):
    monkeypatch.setattr(agent.shutil, "which", lambda _name: None)
    outcome = agent.run_claude_turn(workspace, "add divide", None, 0, out=io.StringIO())
    assert not outcome.ok and "claude" in outcome.error


def test_run_fake_turn_rejects_an_unknown_step_kind(workspace):
    scenario = Scenario(
        "bad-step",
        (),
        "an unknown step kind is a broken turn, not a KeyError",
        (Turn(summary="dance", steps=(Step("dance"),), tests_pass=True, lint_findings=0),),
    )
    outcome = agent.run_fake_turn(workspace, scenario, 1, 0, None, out=io.StringIO())
    assert not outcome.ok
    assert "unknown step kind" in outcome.error
