"""Content-addressed artifact cache.

What: download a .tar.gz once per digest, verify it, extract it atomically.
Why: the orchestrator ships its own code version to the VM as an artifact
pinned by sha256. Caching by digest makes a second turn on the same VM free
and makes a corrupted download impossible to use.
Production: identical. Warm-pool VMs prefetch the current artifact at boot.
"""

import hashlib
import os
import shutil
import tarfile
from pathlib import Path

from sandbox.contract.errors import Incompatible
from sandbox.objectstore import ObjectStore


class ArtifactCache:
    def __init__(self, root: Path, store: ObjectStore) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store

    def ensure(self, uri: str, sha256: str) -> Path:
        target = self.root / sha256
        if (target / ".complete").exists():
            return target
        download = self.root / f".dl-{sha256}-{os.getpid()}"
        self.store.download_file(uri, download)
        digest = hashlib.sha256(download.read_bytes()).hexdigest()
        if digest != sha256:
            download.unlink()
            raise Incompatible(f"artifact {uri} digest {digest} does not match {sha256}")
        staging = self.root / f".tmp-{sha256}-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        with tarfile.open(download, "r:gz") as tar:
            tar.extractall(staging, filter="data")
        download.unlink()
        for path in [staging, *staging.rglob("*")]:
            mode = path.stat().st_mode
            path.chmod(mode | 0o055 if path.is_dir() else mode | 0o044)
        try:
            staging.rename(target)
        except OSError:
            # Another process almost certainly won the race and put its own
            # extraction there. Take theirs if it is finished; otherwise the
            # rename failed for a real reason and nothing here is usable.
            shutil.rmtree(staging, ignore_errors=True)
            if (target / ".complete").exists():
                return target
            raise
        (target / ".complete").touch()
        return target
