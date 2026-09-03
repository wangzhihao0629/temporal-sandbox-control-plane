"""Input validation at the VM boundary.

What: the checks `exec_start`, `put_file`, and `get_file` run on caller input.
Why: the VM does not trust the orchestrator with paths or credentials. Paths
must stay under the workspace root, credential-shaped environment keys are
refused so secrets cannot leak through the non-secret channel, and secrets are
resolved from names to files the worker user owns.
Production: identical.
"""

from pathlib import Path

from sandbox.contract.errors import Incompatible

FORBIDDEN_PREFIXES = ("AWS_",)
FORBIDDEN_SUFFIXES = ("_TOKEN", "_SECRET", "_KEY")


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


def validate_env(env: dict[str, str]) -> dict[str, str]:
    for key in env:
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
