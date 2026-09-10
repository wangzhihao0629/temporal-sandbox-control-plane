"""Package the runner as the artifact a VM downloads.

What: a deterministic `runner-<version>-<sha12>.tar.gz` holding the runner
package, the two modules it imports, and a `bin/runner` launcher; its sha256;
an upload to `s3://sandbox-artifacts/runner/`; and `RUNNER_URI` plus
`RUNNER_SHA256` written into `.env` for `make session`.
Why: the orchestrator names the code version it wants by digest and the VM
verifies it, so what runs on a VM is decided by the workflow, not by whatever
the image happened to be built with. Deterministic bytes mean the same source
always has the same digest, so a rebuild is a cache hit on every VM.
Production: a per-commit virtualenv built in CI; the image only supplies the
interpreter. The demo image already has the dependencies, so the artifact
carries only the runner.

Usage: uv run python -m sandbox.runner.package
"""

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

from sandbox import envfile
from sandbox.objectstore import ObjectStore
from sandbox.runner import RUNNER_VERSION

RUNNER_FILES = ("sandbox/__init__.py", "sandbox/objectstore.py", "sandbox/timeutil.py")
RUNNER_PACKAGE = "sandbox/runner"
ARTIFACT_PREFIX = "s3://sandbox-artifacts/runner"

LAUNCHER = """#!/bin/sh
# Run the runner with the image's interpreter (it has boto3, pytest, ruff), or
# with RUNNER_PYTHON when a test points at another one. The artifact's own copy
# of the package goes first on PYTHONPATH, so the version the orchestrator
# shipped is the version that runs, not whatever the image was built with.
here="$(cd "$(dirname "$0")/.." && pwd)"
py="${RUNNER_PYTHON:-}"
if [ -z "$py" ]; then
  if [ -x /opt/sandbox/venv/bin/python ]; then py=/opt/sandbox/venv/bin/python; else py=python3; fi
fi
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec "$py" -m sandbox.runner "$@"
"""


def members(root: Path) -> list[tuple[str, bytes, int]]:
    out = [(name, (root / name).read_bytes(), 0o644) for name in RUNNER_FILES]
    for path in sorted((root / RUNNER_PACKAGE).glob("*.py")):
        out.append((f"{RUNNER_PACKAGE}/{path.name}", path.read_bytes(), 0o644))
    out.append(("bin/runner", LAUNCHER.encode(), 0o755))
    return sorted(out)


def build(root: Path, out_dir: Path) -> tuple[Path, str]:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for name, data, mode in members(root):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = mode
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    data = buf.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"runner-{RUNNER_VERSION}-{sha[:12]}.tar.gz"
    path.write_bytes(data)
    return path, sha


def publish(store: ObjectStore, path: Path, sha256: str) -> str:
    uri = f"{ARTIFACT_PREFIX}/{path.name}"
    store.upload_file(path, uri)
    store.put_bytes(f"{uri}.sha256", f"{sha256}  {path.name}\n".encode(), "text/plain")
    return uri


def main() -> None:
    envfile.load()
    root = Path(__file__).resolve().parents[2]
    path, sha = build(root, root / "dist")
    uri = publish(ObjectStore.from_env(), path, sha)
    envfile.write(root / ".env", {"RUNNER_URI": uri, "RUNNER_SHA256": sha})
    print(f"artifact {uri}")
    print(f"sha256   {sha}")
    print("wrote RUNNER_URI and RUNNER_SHA256 to .env")


if __name__ == "__main__":
    main()
