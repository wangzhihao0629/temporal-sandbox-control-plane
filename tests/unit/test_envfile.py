"""Reading .env into the process environment without clobbering what is set."""

import os

from sandbox import envfile


def test_load_sets_missing_keys_and_skips_comments(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('# comment\nSANDBOX_TEST_A=1\nSANDBOX_TEST_B="two words"\n\n')
    monkeypatch.delenv("SANDBOX_TEST_A", raising=False)
    monkeypatch.setenv("SANDBOX_TEST_B", "kept")
    loaded = envfile.load(path)
    assert loaded == {"SANDBOX_TEST_A": "1", "SANDBOX_TEST_B": "two words"}
    assert os.environ["SANDBOX_TEST_A"] == "1"
    assert os.environ["SANDBOX_TEST_B"] == "kept"


def test_load_missing_file_is_empty(tmp_path):
    assert envfile.load(tmp_path / "nope") == {}


def test_write_replaces_and_appends_keys_and_keeps_comments(tmp_path):
    from sandbox import envfile

    path = tmp_path / ".env"
    path.write_text("# stack\nA=1\n\nB=two\n# RUNNER_URI=old\n")
    envfile.write(path, {"B": "2", "RUNNER_URI": "s3://x/y", "RUNNER_SHA256": "abc"})
    assert path.read_text() == (
        "# stack\nA=1\n\nB=2\n# RUNNER_URI=old\nRUNNER_URI=s3://x/y\nRUNNER_SHA256=abc\n"
    )
    envfile.write(path, {"RUNNER_SHA256": "def"})
    assert path.read_text().count("RUNNER_SHA256=") == 1 and "RUNNER_SHA256=def" in path.read_text()


def test_write_creates_a_missing_file(tmp_path):
    from sandbox import envfile

    path = tmp_path / "new.env"
    envfile.write(path, {"K": "v"})
    assert path.read_text() == "K=v\n"
