"""The Go build demo's steps: fetch a repo, edit it, build it, run it.

What: `fetch` clones an HTTPS git URL into `<workspace>/repo` at a pinned ref;
`edit` applies one named, scripted edit; `build` runs `go build`; `run` executes
the binary and reports its output. Each returns an envelope like the coding
session's steps.
Why: this is the "a real repository from the internet, compiled and run inside
the VM" path, next to the coding session's offline seed repo. `fetch` accepts
only https and refuses every other git transport (file://, ssh, ext), so a URL
cannot read another path on the VM; what hosts it may reach is the VM's network
policy's job, not this module's. The Go build never downloads anything:
GOPROXY=off, GOTOOLCHAIN=local, and a module with no dependencies, so the only
network use is the clone. Caches live beside the repo, not in it, so a snapshot
of `repo/` carries source and binary and nothing else.
Production: the real agent makes the edits; fetch/build/run keep their shape.
"""

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sandbox.runner.agent import apply_edit
from sandbox.runner.envelopes import (
    BuildEnvelope,
    EditEnvelope,
    FetchEnvelope,
    RunEnvelope,
    truncate,
)
from sandbox.runner.gitutil import git

REPO_DIR = "repo"
BINARY = "bin/app"
GIT_TIMEOUT_SECONDS = 300
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 2.0
# git's wording for failures a retry can fix. Anything else — a missing
# repository, a bad ref, a refused URL — fails on the first attempt.
TRANSIENT_GIT_ERRORS = (
    "Could not resolve host",
    "Failed to connect",
    "Connection timed out",
    "Operation timed out",
    "Connection reset",
    "early EOF",
    "The requested URL returned error: 5",
)


@dataclass(frozen=True)
class GoEdit:
    file: str
    old: str
    new: str


# Scripted edits, keyed by name, against github.com/golang/example at the ref
# the demo pins. A new demo edit is one entry here.
GO_EDITS = {
    "greet-sandbox": GoEdit(
        file="hello/hello.go",
        old='name := "world"',
        new='name := "Temporal sandbox"',
    ),
}


def repo_path(workspace: Path) -> Path:
    return Path(workspace) / REPO_DIR


def fetch(workspace: Path, url: str, ref: str) -> FetchEnvelope:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"only https:// git URLs are allowed, got {url!r}")
    dest = repo_path(workspace)
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    Path(workspace).mkdir(parents=True, exist_ok=True)
    _clone(url, dest, Path(workspace))
    git("checkout", "--quiet", "--detach", ref, cwd=dest)
    head = git("rev-parse", "HEAD", cwd=dest)
    return FetchEnvelope(ok=True, url=url, ref=ref, head=head)


def _clone(url: str, dest: Path, cwd: Path, sleep=time.sleep) -> None:
    """Clone, retrying the failures a flaky network causes and nothing else."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        proc = subprocess.run(
            [
                "git",
                "-c", "protocol.allow=never",
                "-c", "protocol.https.allow=always",
                "clone", "--quiet", "--no-checkout", url, str(dest),
            ],
            cwd=str(cwd),
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if proc.returncode == 0:
            return
        error = proc.stderr.strip()
        transient = any(marker in error for marker in TRANSIENT_GIT_ERRORS)
        if not transient or attempt == FETCH_ATTEMPTS:
            raise RuntimeError(
                f"git clone {url} failed ({proc.returncode}) after {attempt} attempt(s): {error}"
            )
        print(f"[fetch] attempt {attempt} failed, retrying: {error}", flush=True)
        shutil.rmtree(dest, ignore_errors=True)
        sleep(FETCH_BACKOFF_SECONDS * attempt)


def edit(workspace: Path, name: str) -> EditEnvelope:
    if name not in GO_EDITS:
        raise KeyError(f"unknown edit {name!r}; choose from {sorted(GO_EDITS)}")
    spec = GO_EDITS[name]
    stat = apply_edit(repo_path(workspace) / spec.file, spec.old, spec.new)
    if stat is None:
        return EditEnvelope(ok=True, edit=name, file=spec.file, already_applied=True)
    return EditEnvelope(ok=True, edit=name, file=spec.file, added=stat[0], removed=stat[1])


def go_env(workspace: Path) -> dict[str, str]:
    cache = Path(workspace) / ".cache"
    return {
        **os.environ,
        "GOCACHE": str(cache / "go-build"),
        "GOPATH": str(cache / "gopath"),
        "GOTOOLCHAIN": "local",
        "GOPROXY": "off",
        "CGO_ENABLED": "0",
    }


def build(workspace: Path, package: str) -> BuildEnvelope:
    package_dir = repo_path(workspace) / package
    if not package_dir.is_dir():
        raise FileNotFoundError(f"no package directory {package_dir}")
    binary = repo_path(workspace) / BINARY
    env = go_env(workspace)
    version = subprocess.run(
        ["go", "env", "GOVERSION"], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()
    started = time.monotonic()
    # -buildvcs=false: stamping reads git state, and a restored snapshot's .git is
    # owned by the worker, not the job user, so git would refuse it.
    proc = subprocess.run(
        ["go", "build", "-buildvcs=false", "-o", str(binary), "."],
        cwd=str(package_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    seconds = round(time.monotonic() - started, 2)
    if proc.returncode != 0:
        raise RuntimeError(f"go build failed ({proc.returncode}): {proc.stderr.strip()}")
    return BuildEnvelope(
        ok=True,
        binary=str(binary),
        bytes=binary.stat().st_size,
        go_version=version,
        seconds=seconds,
    )


def run(workspace: Path, args: list[str], timeout: float = 60) -> RunEnvelope:
    binary = repo_path(workspace) / BINARY
    if not binary.is_file():
        raise FileNotFoundError(f"no binary at {binary}; build first")
    proc = subprocess.run(
        [str(binary), *args],
        cwd=str(repo_path(workspace)),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return RunEnvelope(
        ok=True,
        exit_code=proc.returncode,
        stdout=truncate(proc.stdout.strip()),
        stderr=truncate(proc.stderr.strip()),
    )
