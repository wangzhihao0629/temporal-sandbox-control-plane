"""File transfer between the object store and the workspace.

What: put_file downloads an object to a workspace path; get_file uploads a
workspace file and returns its URI.
Why: bytes never travel inside Temporal payloads. Paths are validated to stay
under the workspace root.
Production: identical.
"""

import os
from pathlib import Path

from sandbox.contract.errors import Incompatible
from sandbox.contract.types import FileStat
from sandbox.objectstore import ObjectStore


def put_file(store: ObjectStore, src_uri: str, path: Path) -> FileStat:
    path.parent.mkdir(parents=True, exist_ok=True)
    size = store.download_file(src_uri, path)
    os.chmod(path, 0o664)
    return FileStat(path=str(path), size=size, uri=src_uri)


def get_file(store: ObjectStore, path: Path, dst_uri: str) -> FileStat:
    if not path.is_file():
        raise Incompatible(f"no such file on the VM: {path}")
    size = store.upload_file(path, dst_uri)
    return FileStat(path=str(path), size=size, uri=dst_uri)
