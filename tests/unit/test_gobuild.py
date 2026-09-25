"""The Go build demo's runner steps. The clone and a real build are exercised on
a real VM by `make gobuild`; these cover the rules around them."""

import shutil

import pytest

from sandbox.runner import envelopes, gitutil, gobuild


def test_fetch_accepts_only_https(tmp_path):
    for url in ["file:///etc", "ssh://git@github.com/golang/example", "git://x/y", "http://x/y"]:
        with pytest.raises(ValueError, match="only https"):
            gobuild.fetch(tmp_path, url, "main")
    assert not (tmp_path / "repo").exists()


def test_fetch_refuses_to_clone_over_an_existing_repo(tmp_path):
    (tmp_path / "repo").mkdir()
    with pytest.raises(FileExistsError):
        gobuild.fetch(tmp_path, "https://github.com/golang/example", "main")


def _hello(tmp_path):
    src = tmp_path / "repo" / "hello"
    src.mkdir(parents=True)
    (src / "hello.go").write_text('package main\n\nfunc main() {\n\tname := "world"\n}\n')
    return src / "hello.go"


def test_edit_applies_once_and_reports_a_repeat_as_already_applied(tmp_path):
    path = _hello(tmp_path)
    first = gobuild.edit(tmp_path, "greet-sandbox")
    assert first.ok and first.added == 1 and first.removed == 1
    assert 'name := "Temporal sandbox"' in path.read_text()
    again = gobuild.edit(tmp_path, "greet-sandbox")
    assert again.ok and again.already_applied


def test_edit_rejects_an_unknown_edit_name(tmp_path):
    _hello(tmp_path)
    with pytest.raises(KeyError):
        gobuild.edit(tmp_path, "nope")


def test_run_reports_output_and_exit_code_as_data(tmp_path):
    binary = tmp_path / "repo" / gobuild.BINARY
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\necho "args: $*"\nexit 3\n')
    binary.chmod(0o755)
    result = gobuild.run(tmp_path, ["-r", "x"])
    assert result.ok and result.exit_code == 3 and result.stdout == "args: -r x"


def test_run_before_build_is_a_broken_step(tmp_path):
    with pytest.raises(FileNotFoundError, match="build first"):
        gobuild.run(tmp_path, [])


def test_go_env_never_downloads_and_shares_one_cache_per_vm(tmp_path):
    workspace = tmp_path / "wf" / "build-1"
    env = gobuild.go_env(workspace, {"SANDBOX_WORKSPACE_ROOT": str(tmp_path)})
    assert env["GOPROXY"] == "off" and env["GOTOOLCHAIN"] == "local"
    assert env["GOCACHE"] == str(tmp_path / ".cache" / "go-build"), "at the root, not per attempt"
    assert gobuild.go_env(workspace, {})["GOCACHE"].startswith(str(workspace))


@pytest.mark.skipif(shutil.which("go") is None, reason="no Go toolchain on this machine")
def test_build_then_run_a_real_program(tmp_path):
    src = tmp_path / "repo" / "hello"
    src.mkdir(parents=True)
    (src / "go.mod").write_text("module example.com/hello\n\ngo 1.19\n")
    program = 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("hi") }\n'
    (src / "main.go").write_text(program)
    built = gobuild.build(tmp_path, "hello")
    assert built.ok and built.bytes > 0 and built.go_version.startswith("go")
    assert gobuild.run(tmp_path, []).stdout == "hi"


def test_every_new_step_has_an_envelope_kind():
    for kind in ("fetch", "edit", "build", "run"):
        assert kind in envelopes.KINDS
        assert envelopes.broken(kind, "boom").kind == kind


class _FakeGit:
    """Stands in for subprocess.run on `git clone`: fails with the given stderr
    lines in turn, then succeeds."""

    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def __call__(self, argv, **kwargs):
        import subprocess

        self.calls += 1
        if self.failures:
            return subprocess.CompletedProcess(argv, 128, "", self.failures.pop(0))
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_fetch_retries_a_transient_network_failure_then_succeeds(tmp_path, monkeypatch):
    fake = _FakeGit(["fatal: unable to access: Could not resolve host: github.com"])
    monkeypatch.setattr(gitutil.subprocess, "run", fake)
    slept = []
    gobuild._clone("https://github.com/x/y", tmp_path / "repo", tmp_path, sleep=slept.append)
    assert fake.calls == 2 and slept == [gobuild.FETCH_BACKOFF_SECONDS]


def test_fetch_gives_up_after_the_last_attempt(tmp_path, monkeypatch):
    fake = _FakeGit(["fatal: Connection reset by peer"] * gobuild.FETCH_ATTEMPTS)
    monkeypatch.setattr(gitutil.subprocess, "run", fake)
    with pytest.raises(gitutil.GitError, match=f"after {gobuild.FETCH_ATTEMPTS} attempt"):
        gobuild._clone("https://github.com/x/y", tmp_path / "repo", tmp_path, sleep=lambda s: None)
    assert fake.calls == gobuild.FETCH_ATTEMPTS


def test_fetch_does_not_retry_a_permanent_failure(tmp_path, monkeypatch):
    fake = _FakeGit(["remote: Repository not found.\nfatal: repository not found"])
    monkeypatch.setattr(gitutil.subprocess, "run", fake)
    with pytest.raises(gitutil.GitError, match="after 1 attempt"):
        gobuild._clone("https://github.com/x/y", tmp_path / "repo", tmp_path, sleep=lambda s: None)
    assert fake.calls == 1
