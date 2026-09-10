"""The runner command line.

What: argparse over five subcommands; every one writes its envelope to
`--envelope-uri`, including when it raised, and exits 1 only then.
Why: the orchestrator treats a non-zero exit as "the step broke" and the
envelope as the step's answer, so both must be written no matter what
happened. Failing tests and lint findings exit 0.
Production: identical; the real harness adds subcommands, not exit codes.
"""

import argparse
import os
import sys
import traceback
from pathlib import Path

from sandbox.objectstore import ObjectStore
from sandbox.runner import checks, envelopes, session


def cmd_clone(args, store):
    return session.clone(
        store, args.repo, Path(args.workspace), args.session_uri, Path(args.seed_root)
    )


def cmd_turn(args, store):
    return session.run_turn(
        store,
        Path(args.workspace),
        args.session_uri,
        args.prompt,
        args.scenario,
        args.feedback_uri,
        args.seconds,
        args.agent,
    )


def cmd_lint(args, store):
    return checks.run_ruff(Path(args.workspace))


def cmd_test(args, store):
    return checks.run_pytest(Path(args.workspace))


def cmd_export(args, store):
    return session.export(store, Path(args.workspace), args.session_uri)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runner")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--workspace", required=True)
        p.add_argument("--envelope-uri", required=True)

    p = sub.add_parser("clone")
    common(p)
    p.add_argument("--repo", default="hello")
    p.add_argument("--session-uri", required=True)
    p.add_argument("--seed-root", default=os.environ.get("SEED_REPOS_DIR", "/srv/repos"))
    p.set_defaults(func=cmd_clone)

    p = sub.add_parser("turn")
    common(p)
    p.add_argument("--session-uri", required=True)
    p.add_argument("--prompt", default="")
    p.add_argument("--scenario", default="")
    p.add_argument("--feedback-uri", default="none")
    p.add_argument("--seconds", type=float, default=0.0)
    p.add_argument("--agent", default="fake", choices=("fake", "claude"))
    p.set_defaults(func=cmd_turn)

    p = sub.add_parser("lint")
    common(p)
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("test")
    common(p)
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("export")
    common(p)
    p.add_argument("--session-uri", required=True)
    p.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = ObjectStore.from_env()
    try:
        envelope = args.func(args, store)
    except Exception as e:  # a broken step is still reported as an envelope
        traceback.print_exc()
        envelope = envelopes.broken(args.command, f"{type(e).__name__}: {e}")
    store.put_json(args.envelope_uri, envelope.to_dict())
    print(f"[runner] {args.command}: {'ok' if envelope.ok else 'broken'}", flush=True)
    return 0 if envelope.ok else 1


if __name__ == "__main__":
    sys.exit(main())
