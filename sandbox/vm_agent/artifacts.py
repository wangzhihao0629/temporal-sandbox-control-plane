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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_tarball(tarball: Path, dest: Path, dir_bits: int, file_bits: int) -> int:
    """Unpack a .tar.gz into `dest` and return how many members it held.

    Every member goes through tarfile's `data` filter, which refuses anything
    that would land outside `dest`. The mode bits are added in the same pass, so
    there is no second walk over the tree afterwards.
    """
    # A set, not a counter: tarfile runs the filter twice on directories, once to
    # create them and again when it applies their modes at the end.
    names: set[str] = set()

    def with_bits(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo:
        member = tarfile.data_filter(member, path)
        names.add(member.name)
        # The data filter leaves a directory's mode as None, which tarfile
        # creates as 0o700; add to that, as the old walk after extraction did.
        mode = 0o700 if member.mode is None else member.mode
        if member.isdir():
            return member.replace(mode=mode | dir_bits, deep=False)
        if member.isfile():
            return member.replace(mode=mode | file_bits, deep=False)
        return member

    with tarfile.open(tarball, "r:gz") as tar:
        tar.extractall(dest, filter=with_bits)
    dest.chmod(dest.stat().st_mode | dir_bits)
    return len(names)


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
        digest = sha256_file(download)
        if digest != sha256:
            download.unlink()
            raise Incompatible(f"artifact {uri} digest {digest} does not match {sha256}")
        staging = self.root / f".tmp-{sha256}-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        # Readable (and traversable) by everyone: the job user runs what is in here.
        extract_tarball(download, staging, dir_bits=0o055, file_bits=0o044)
        download.unlink()
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
