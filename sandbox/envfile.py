"""Load a .env file into the process environment.

What: KEY=VALUE lines, comments and blanks skipped, quotes stripped.
Why: `make up` writes endpoint addresses into .env; every host process reads
them the same way without adding a dependency.
Production: not used. Workers get their environment from the platform.
"""

import os
from pathlib import Path


def load(path: str | os.PathLike = ".env", override: bool = False) -> dict[str, str]:
    file = Path(path)
    if not file.exists():
        return {}
    loaded: dict[str, str] = {}
    for raw in file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded
