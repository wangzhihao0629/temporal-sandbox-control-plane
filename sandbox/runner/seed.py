"""Turn a seed directory into a bare repository.

What: `make_bare_repo(src, dst)` commits a copy of `src` on `main` and clones it
bare into `dst`; the CLI does it for every directory under a root.
Why: the image bakes `/srv/repos/<name>.git` from `images/vm/seed/` so the demo
works offline, and the tests build the same repos in a temp directory, so one
implementation serves both.
Production: the seed is a real repository fetched with a token.

Usage: python -m sandbox.runner.seed <src_root> <dst_root>
"""

import shutil
import sys
import tempfile
from pathlib import Path

from sandbox.runner.gitutil import git


def make_bare_repo(src: Path, dst: Path) -> str:
    # Absolute on purpose: the clone below runs with the temp dir as cwd.
    src, dst = Path(src).resolve(), Path(dst).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "work"
        shutil.copytree(src, work)
        git("init", "-q", "-b", "main", cwd=work)
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", "seed", cwd=work)
        head = git("rev-parse", "HEAD", cwd=work)
        if dst.exists():
            shutil.rmtree(dst)
        git("clone", "-q", "--bare", str(work), str(dst), cwd=tmp)
    return head


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m sandbox.runner.seed <src_root> <dst_root>", file=sys.stderr)
        return 2
    src_root, dst_root = Path(args[0]), Path(args[1])
    for child in sorted(p for p in src_root.iterdir() if p.is_dir()):
        head = make_bare_repo(child, dst_root / f"{child.name}.git")
        print(f"{dst_root / (child.name + '.git')} @ {head[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
