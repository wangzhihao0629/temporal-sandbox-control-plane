"""VM agent boundary validation: workspace paths, env keys, and secret names."""

from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError

from sandbox.vm_agent import validation


def test_validate_cwd_accepts_paths_under_root_and_rejects_escapes(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    assert validation.validate_cwd(str(root / "wf-1"), root) == root / "wf-1"
    with pytest.raises(ApplicationError) as err:
        validation.validate_cwd(str(tmp_path / "elsewhere"), root)
    assert err.value.type == "Incompatible"
    with pytest.raises(ApplicationError):
        validation.validate_cwd(str(root / ".." / "x"), root)


@pytest.mark.parametrize("key", ["AWS_ACCESS_KEY_ID", "GITHUB_TOKEN", "MY_SECRET", "API_KEY"])
def test_validate_env_rejects_credential_shaped_keys(key):
    with pytest.raises(ApplicationError) as err:
        validation.validate_env({key: "x"})
    assert err.value.type == "Incompatible"


def test_validate_env_passes_ordinary_keys():
    assert validation.validate_env({"PROMPT": "hi", "TURN_SECONDS": "3"}) == {
        "PROMPT": "hi",
        "TURN_SECONDS": "3",
    }


def test_resolve_secrets_reads_files_and_rejects_unknown_names(tmp_path):
    (tmp_path / "github_token").write_text("ghs_abc\n")
    assert validation.resolve_secrets(["github_token"], tmp_path) == {"GITHUB_TOKEN": "ghs_abc"}
    with pytest.raises(ApplicationError) as err:
        validation.resolve_secrets(["nope"], tmp_path)
    assert err.value.type == "Incompatible"
    with pytest.raises(ApplicationError):
        validation.resolve_secrets(["../etc/passwd"], tmp_path)
    assert validation.resolve_secrets([], Path("/nonexistent")) == {}
