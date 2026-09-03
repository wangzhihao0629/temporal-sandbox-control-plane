# tests/unit/test_envfile.py
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
