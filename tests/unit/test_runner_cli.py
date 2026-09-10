"""The runner CLI end to end in-process: clone, turn, lint, test, export, recovery, resume."""

from pathlib import Path

import pytest

from sandbox.runner import session
from sandbox.runner.cli import main
from sandbox.runner.gitutil import git
from sandbox.runner.seed import make_bare_repo

SEED = Path(__file__).resolve().parents[2] / "images" / "vm" / "seed" / "hello"
SESSION = "s3://sandbox-sessions/s1"


@pytest.fixture
def store(s3_store, monkeypatch):
    # ObjectStore.from_env() inside main() must build a client moto's mock sees:
    # an explicit local endpoint would bypass the mock and hit the network.
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    return s3_store


@pytest.fixture
def seed_root(tmp_path):
    root = tmp_path / "repos"
    make_bare_repo(SEED, root / "hello.git")
    return root


def _env(store, name):
    return store.get_json(f"{SESSION}/steps/{name}.json")


def _clone(ws, seed_root, name="clone"):
    return main(
        [
            "clone",
            "--repo",
            "hello",
            "--workspace",
            str(ws),
            "--session-uri",
            SESSION,
            "--seed-root",
            str(seed_root),
            "--envelope-uri",
            f"{SESSION}/steps/{name}.json",
        ]
    )


def _turn(ws, name, scenario="multiply-with-bug", feedback="none"):
    return main(
        [
            "turn",
            "--workspace",
            str(ws),
            "--session-uri",
            SESSION,
            "--prompt",
            "add multiply",
            "--scenario",
            scenario,
            "--feedback-uri",
            feedback,
            "--seconds",
            "0",
            "--envelope-uri",
            f"{SESSION}/steps/{name}.json",
        ]
    )


def _step(ws, kind, name, *extra):
    return main(
        [kind, "--workspace", str(ws), *extra, "--envelope-uri", f"{SESSION}/steps/{name}.json"]
    )


def test_a_full_session_with_a_fix_loop(store, seed_root, tmp_path):
    ws = tmp_path / "ws"
    assert _clone(ws, seed_root) == 0
    clone = _env(store, "clone")
    assert clone["ok"] and clone["source"] == "seed" and clone["turn"] == 0
    assert clone["kind"] == "clone" and (ws / "calc" / "__init__.py").exists()

    assert _turn(ws, "turn1") == 0
    turn1 = _env(store, "turn1")
    assert turn1["turn"] == 1 and turn1["files_changed"] == 2 and not turn1["feedback_used"]
    assert turn1["fake_cost_usd"] > 0 and turn1["scenario"] == "multiply-with-bug"
    assert store.exists(f"{SESSION}/repo.bundle")
    assert store.get_json(f"{SESSION}/session.json")["turn"] == 1

    assert _step(ws, "lint", "lint1") == 0 and _env(store, "lint1")["count"] == 0
    assert _step(ws, "test", "test1") == 0, "failing tests are data, not a broken step"
    test1 = _env(store, "test1")
    assert test1["failed"] == 1 and test1["failures"][0]["test"].endswith("test_multiply")

    assert _turn(ws, "turn2", feedback=f"{SESSION}/steps/test1.json") == 0
    assert _env(store, "turn2")["turn"] == 2 and _env(store, "turn2")["feedback_used"]
    assert _step(ws, "test", "test2") == 0 and _env(store, "test2")["failed"] == 0

    assert _step(ws, "export", "export", "--session-uri", SESSION) == 0
    export = _env(store, "export")
    assert export["commits"] == 2 and export["bytes"] > 0
    patch = (ws / "session.patch").read_text()
    assert "Subject: [PATCH 1/2] turn 1:" in patch and "+    return a * b" in patch


def test_clone_resumes_from_the_bundle_on_a_fresh_workspace(store, seed_root, tmp_path):
    first = tmp_path / "a"
    assert _clone(first, seed_root) == 0 and _turn(first, "t1") == 0
    second = tmp_path / "b"
    second.mkdir()
    (second / "leftover.txt").write_text("from an earlier lease")
    assert _clone(second, seed_root, "clone2") == 0
    clone = _env(store, "clone2")
    assert clone["source"] == "bundle" and clone["turn"] == 1
    assert not (second / "leftover.txt").exists(), "clone empties the workspace first"
    assert "def multiply" in (second / "calc" / "__init__.py").read_text()
    assert _turn(second, "t2", feedback="none") == 0 and _env(store, "t2")["turn"] == 2


def test_turn_recovers_a_commit_the_state_missed(store, seed_root, tmp_path):
    ws = tmp_path / "ws"
    assert _clone(ws, seed_root) == 0 and _turn(ws, "t1") == 0
    # The bundle got ahead of session.json: roll the state back one turn.
    state = session.load_state(store, SESSION)
    state.turn = 0
    session.save_state(store, SESSION, state)
    assert _turn(ws, "t1-again") == 0
    again = _env(store, "t1-again")
    assert again["turn"] == 1 and again["summary"] == "recovered from bundle"
    assert again["commit"] == _env(store, "t1")["commit"]
    assert session.load_state(store, SESSION).turn == 1


def test_lint_only_reports_a_finding_and_green_tests(store, seed_root, tmp_path):
    ws = tmp_path / "ws"
    assert _clone(ws, seed_root) == 0
    assert _turn(ws, "t1", scenario="lint-only") == 0
    assert _step(ws, "lint", "l1") == 0
    lint = _env(store, "l1")
    assert lint["count"] == 1 and lint["findings"][0]["code"] == "F401"
    assert lint["findings"][0]["file"] == "calc/__init__.py"
    assert _step(ws, "test", "te1") == 0 and _env(store, "te1")["failed"] == 0


def test_a_broken_step_writes_an_envelope_and_exits_one(store, seed_root, tmp_path):
    ws = tmp_path / "ws"
    rc = _turn(ws, "broken")  # no clone, so no session state
    assert rc == 1
    env = _env(store, "broken")
    assert not env["ok"] and env["kind"] == "turn" and "clone first" in env["error"]
    rc = _clone(ws, seed_root / "missing")
    assert rc == 1 and not _env(store, "clone")["ok"]


def test_seed_repo_is_bare_with_one_commit_on_main(seed_root):
    bare = seed_root / "hello.git"
    assert (bare / "HEAD").read_text().strip() == "ref: refs/heads/main"
    assert git("rev-list", "--count", "HEAD", cwd=bare) == "1"
    author = git("log", "--format=%an <%ae>", "-n", "1", cwd=bare)
    assert author == "fake-agent <fake-agent@sandbox.local>"
