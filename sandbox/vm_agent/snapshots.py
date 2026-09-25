"""Workspace snapshots: a directory to a tarball in the object store, and back.

What: `snapshot` packs a directory under the workspace root into a gzipped tar,
uploads it, and returns its digest; `restore` downloads one, checks the digest,
and unpacks it at a path that must not exist yet, on this VM or any other.
Why: a portable snapshot needs nothing from the provider, so it works the same
on Apple `container`, a Modal sandbox, or EC2. It captures files — source, build
output — not running processes, installed packages, or anything outside the
directory; a provider that can snapshot a whole VM does that as an optional
extra, not instead. The worker (`sandbox-agent`) does the packing, so it reads
what jobs wrote through its `agent` group membership, and a restored tree is
made group-writable so the next job, running as `agent`, can build in it.
Production: identical; a large workspace would stream multipart instead of
building the tarball in a temp file.
"""

import hashlib
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from sandbox.contract.errors import Incompatible
from sandbox.contract.types import SnapshotRef
from sandbox.objectstore import ObjectStore

MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_group_writable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(mode | (0o2070 if path.is_dir() else 0o060))


def snapshot(store: ObjectStore, path: Path, dst_uri: str) -> SnapshotRef:
    if not path.is_dir():
        raise Incompatible(f"no such directory on the VM: {path}")
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "snapshot.tar.gz"
        files = 0
        try:
            with tarfile.open(tarball, "w:gz") as tar:
                for child in sorted(path.rglob("*")):
                    tar.add(child, arcname=str(child.relative_to(path)), recursive=False)
                    files += 1
        except PermissionError as e:
            raise Incompatible(f"snapshot of {path} cannot read {e.filename}") from e
        size = tarball.stat().st_size
        if size > MAX_SNAPSHOT_BYTES:
            raise Incompatible(
                f"snapshot of {path} is {size} bytes; the cap is {MAX_SNAPSHOT_BYTES}"
            )
        sha256 = _sha256(tarball)
        store.upload_file(tarball, dst_uri)
    return SnapshotRef(uri=dst_uri, sha256=sha256, size=size, files=files, path=str(path))


def restore(store: ObjectStore, src_uri: str, sha256: str, path: Path) -> SnapshotRef:
    if path.exists():
        raise Incompatible(f"restore target {path} already exists; restore into a fresh path")
    parent = path.parent
    missing = [p for p in [parent, *parent.parents] if not p.exists()]
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise Incompatible(f"restore cannot create {parent}: {e}") from e
    for created in missing:
        # Jobs run as `agent`, not as the worker that created these: without
        # group write they could read the restored tree but never build in it.
        created.chmod(0o2775)
    if not os.access(parent, os.W_OK):
        raise Incompatible(
            f"restore cannot write into {parent}; restore before any job has run in it"
        )
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "snapshot.tar.gz"
        size = store.download_file(src_uri, tarball)
        actual = _sha256(tarball)
        if actual != sha256:
            raise Incompatible(f"snapshot {src_uri} digest {actual} does not match {sha256}")
        staging = parent / f".restore-{path.name}-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        with tarfile.open(tarball, "r:gz") as tar:
            members = tar.getmembers()
            tar.extractall(staging, filter="data")
        _make_group_writable(staging)
        staging.rename(path)
    return SnapshotRef(uri=src_uri, sha256=sha256, size=size, files=len(members), path=str(path))
