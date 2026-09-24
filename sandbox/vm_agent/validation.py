"""Input validation at the VM boundary.

What: the checks `exec_start`, `put_file`, and `get_file` run on caller input.
Why: the VM does not trust the orchestrator with paths, argv, or credentials.
Paths must stay under the workspace root, argv must match the exec policy so
a workflow cannot ask this VM to run anything else, credential-shaped
environment keys are refused so secrets cannot leak through the non-secret
channel, keys that steer a loader or a shell before the job's own code runs
are refused because this environment is handed to a privileged wrapper, and
secrets are resolved from names to files the worker user owns.
Production: identical.
"""

import re
from pathlib import Path

from sandbox.contract.errors import Incompatible
from sandbox.contract.exec_policy import ExecPolicy

# `LD_`/`DYLD_` preload and library paths execute attacker code inside whatever
# the wrapper runs, sudo included; `BASH_ENV`/`ENV` are sourced by a
# non-interactive shell before its first command.
_WRAPPER_PREFIXES = ("LD_", "DYLD_")
FORBIDDEN_PREFIXES = ("AWS_", *_WRAPPER_PREFIXES)
FORBIDDEN_SUFFIXES = ("_TOKEN", "_SECRET", "_KEY")
FORBIDDEN_KEYS = ("BASH_ENV", "ENV")

# A key with `=` in it would split into a second variable inside Popen's
# environment, and a key with `,` would forge an extra name in sudo's
# --preserve-env list. Only real shell identifiers cross.
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_path_under(path: str, root: Path) -> Path:
    root = root.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        raise Incompatible(f"path must be absolute: {path}")
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise Incompatible(f"path {path} is outside {root}")
    return resolved


def validate_cwd(cwd: str, root: Path) -> Path:
    return validate_path_under(cwd, root)


def validate_argv(argv: list[str], policy: ExecPolicy) -> list[str]:
    policy.check(argv)
    return argv


def validate_env(env: dict[str, str]) -> dict[str, str]:
    for key in env:
        if not _KEY.match(key):
            raise Incompatible(f"env key {key!r} is not a valid environment variable name")
        if key in FORBIDDEN_KEYS or key.startswith(_WRAPPER_PREFIXES):
            raise Incompatible(
                f"env key {key!r} steers the privileged wrapper; it cannot come from a caller"
            )
        if key.startswith(FORBIDDEN_PREFIXES) or key.endswith(FORBIDDEN_SUFFIXES):
            raise Incompatible(
                f"env key {key!r} looks like a credential; pass it as a secret name instead"
            )
    return dict(env)


def resolve_secrets(names: list[str], secrets_dir: Path) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for name in names:
        if not name or "/" in name or name.startswith("."):
            raise Incompatible(f"invalid secret name {name!r}")
        file = secrets_dir / name
        if not file.is_file():
            raise Incompatible(f"unknown secret {name!r}")
        resolved[name.upper()] = file.read_text().strip()
    return resolved
