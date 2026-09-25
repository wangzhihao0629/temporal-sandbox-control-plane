"""Run git with a fixed identity.

What: `git(*args, cwd)` returning stripped stdout, raising GitError otherwise.
Why: the runner commits on machines with no git config, so the identity travels
in the environment; every caller gets the same author and the same error shape.
Production: identical.
"""

import os
import subprocess
from pathlib import Path

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "fake-agent",
    "GIT_AUTHOR_EMAIL": "fake-agent@sandbox.local",
    "GIT_COMMITTER_NAME": "fake-agent",
    "GIT_COMMITTER_EMAIL": "fake-agent@sandbox.local",
    "GIT_TERMINAL_PROMPT": "0",
}


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path | str, timeout: float | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={**os.environ, **GIT_IDENTITY},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()
