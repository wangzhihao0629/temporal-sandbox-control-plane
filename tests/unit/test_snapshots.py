"""Workspace snapshots: pack a directory, restore it elsewhere, refuse the unsafe cases."""

import hashlib
import io
import stat
import tarfile

import pytest
from temporalio.exceptions import ApplicationError

from sandbox.vm_agent import snapshots

URI = "s3://sandbox-sessions/s1/snapshots/repo.tar.gz"


def _tree(root):
    (root / "hello").mkdir(parents=True)
    (root / "hello" / "hello.go").write_text('package main\n\nfunc main() { println("hi") }\n')
    (root / "bin").mkdir()
    binary = root / "bin" / "app"
    binary.write_bytes(b"\x7fELF fake binary")
    binary.chmod(0o755)
    return root


def test_snapshot_then_restore_reproduces_the_tree_on_another_path(s3_store, tmp_path):
    src = _tree(tmp_path / "vm1" / "ws" / "repo")
    ref = snapshots.snapshot(s3_store, src, URI)
    assert ref.uri == URI and len(ref.sha256) == 64 and ref.files == 4 and ref.size > 0

    dst = tmp_path / "vm2" / "ws" / "repo"
    restored = snapshots.restore(s3_store, URI, ref.sha256, dst)
    assert restored.files == 4
    assert (dst / "hello" / "hello.go").read_text() == (src / "hello" / "hello.go").read_text()
    assert (dst / "bin" / "app").stat().st_mode & stat.S_IXUSR, "the binary stays executable"


def test_a_restored_tree_is_group_writable_so_the_job_user_can_build_in_it(s3_store, tmp_path):
    src = _tree(tmp_path / "a" / "repo")
    ref = snapshots.snapshot(s3_store, src, URI)
    dst = tmp_path / "b" / "ws" / "repo"
    snapshots.restore(s3_store, URI, ref.sha256, dst)
    for path in [dst, dst / "hello", dst / "hello" / "hello.go", dst.parent]:
        assert path.stat().st_mode & stat.S_IWGRP, path


def test_restore_refuses_a_digest_that_does_not_match(s3_store, tmp_path):
    ref = snapshots.snapshot(s3_store, _tree(tmp_path / "a" / "repo"), URI)
    with pytest.raises(ApplicationError) as err:
        snapshots.restore(s3_store, URI, "f" * 64, tmp_path / "b" / "repo")
    assert err.value.type == "Incompatible" and ref.sha256 not in "f" * 64
    assert not (tmp_path / "b" / "repo").exists()


def test_restore_refuses_to_overwrite_an_existing_path(s3_store, tmp_path):
    src = _tree(tmp_path / "a" / "repo")
    ref = snapshots.snapshot(s3_store, src, URI)
    with pytest.raises(ApplicationError) as err:
        snapshots.restore(s3_store, URI, ref.sha256, src)
    assert err.value.type == "Incompatible"


def test_snapshot_of_a_missing_directory_is_incompatible(s3_store, tmp_path):
    with pytest.raises(ApplicationError) as err:
        snapshots.snapshot(s3_store, tmp_path / "nope", URI)
    assert err.value.type == "Incompatible"


def test_a_tarball_that_escapes_its_directory_is_refused(s3_store, tmp_path):
    # A snapshot is whatever is at the URI, so restore must not trust its paths.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"pwned\n"
        info = tarfile.TarInfo("../../escaped.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    s3_store.put_bytes(URI, buf.getvalue())
    digest = hashlib.sha256(buf.getvalue()).hexdigest()
    with pytest.raises(tarfile.OutsideDestinationError):
        snapshots.restore(s3_store, URI, digest, tmp_path / "ws" / "repo")
    assert not (tmp_path / "escaped.txt").exists()
