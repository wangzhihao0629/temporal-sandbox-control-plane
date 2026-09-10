"""The runner artifact is deterministic, complete, launchable, and extracts through the VM cache."""

import io
import tarfile
from pathlib import Path

from sandbox.runner import package
from sandbox.vm_agent.artifacts import ArtifactCache

ROOT = Path(__file__).resolve().parents[2]


def test_build_is_deterministic_and_names_the_digest(tmp_path):
    path1, sha1 = package.build(ROOT, tmp_path / "a")
    path2, sha2 = package.build(ROOT, tmp_path / "b")
    assert sha1 == sha2 and path1.read_bytes() == path2.read_bytes()
    assert path1.name == f"runner-{package.RUNNER_VERSION}-{sha1[:12]}.tar.gz"


def test_tar_holds_the_runner_its_imports_and_a_launcher(tmp_path):
    path, _ = package.build(ROOT, tmp_path)
    with tarfile.open(path, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
    assert "sandbox/runner/cli.py" in members and "sandbox/runner/__main__.py" in members
    assert "sandbox/objectstore.py" in members and "sandbox/__init__.py" in members
    assert "sandbox/vm_agent/activities.py" not in members, "the VM side is not shipped"
    assert members["bin/runner"].mode == 0o755
    assert all(m.mtime == 0 and m.uid == 0 for m in members.values())
    assert list(members) == sorted(members)


def test_publish_uploads_the_tarball_and_a_sidecar(tmp_path, s3_store):
    path, sha = package.build(ROOT, tmp_path)
    uri = package.publish(s3_store, path, sha)
    assert uri == f"{package.ARTIFACT_PREFIX}/{path.name}"
    assert s3_store.get_bytes(uri) == path.read_bytes()
    assert s3_store.get_bytes(f"{uri}.sha256").decode().split()[0] == sha


def test_artifact_extracts_through_the_vm_cache(tmp_path, s3_store):
    path, sha = package.build(ROOT, tmp_path / "dist")
    uri = package.publish(s3_store, path, sha)
    extracted = ArtifactCache(tmp_path / "cache", s3_store).ensure(uri, sha)
    launcher = (extracted / "bin" / "runner").read_text()
    assert launcher.startswith("#!/bin/sh") and "RUNNER_PYTHON" in launcher
    assert (extracted / "sandbox" / "runner" / "cli.py").exists()


def test_launcher_text_is_what_the_tar_carries(tmp_path):
    path, _ = package.build(ROOT, tmp_path)
    with tarfile.open(path, "r:gz") as tar:
        data = tar.extractfile("bin/runner").read()
    assert data == package.LAUNCHER.encode()
    assert io.BytesIO(data).read(9) == b"#!/bin/sh"
