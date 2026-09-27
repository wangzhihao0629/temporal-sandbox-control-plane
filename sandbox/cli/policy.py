"""Show or edit a pool's policy.

What: with no flags, print the current policy; with `--min-idle`, `--max`, or
`--image`, merge the change into it and write it back.
Why: the reconciler reads the policy item on its next pass, so a change here
takes effect within one interval — the two knobs a demo needs to watch top-up
and scale-in react without editing the registry by hand.
Production: the dashboard's policy endpoint does the same write.

Usage: uv run python -m sandbox.cli.policy [--pool demo] [--min-idle N]
    [--max N] [--image I]
"""

import argparse

from sandbox import envfile
from sandbox.manager.policy import merge_policy
from sandbox.registry.client import Registry


def main() -> None:
    envfile.load()
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", default="demo")
    parser.add_argument("--min-idle", type=int)
    parser.add_argument("--max", type=int)
    parser.add_argument("--image")
    args = parser.parse_args()
    registry = Registry.from_env()
    current = registry.get_policy(args.pool)
    if current is None:
        raise SystemExit(
            f"pool {args.pool!r} has no policy; run `uv run python -m sandbox.bootstrap`"
        )
    if any(v is not None for v in (args.min_idle, args.max, args.image)):
        merged = merge_policy(current, args.min_idle, args.max, args.image)
        current = registry.put_policy(args.pool, **merged)
    print(
        f"pool {current['pool']}: min_idle={current['min_idle']} max={current['max']} "
        f"image={current['image']} cpus={current['cpus']} memory={current['memory']}"
    )


if __name__ == "__main__":
    main()
