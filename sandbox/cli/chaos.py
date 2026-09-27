"""`make chaos-kill|chaos-stop|chaos-delete VM=<id>`: break a VM on purpose.

What: parses the action and VM id and calls `sandbox.manager.chaos.apply`
against the Apple `container` provider.
Why: the drills in docs/08-running-the-demo.md need a one-line way to cause
each kind of host failure.
Production: an operator action; the dashboard's chaos endpoint does the same.

Usage: uv run python -m sandbox.cli.chaos {kill|stop|delete} <vm_id>
"""

import os
import sys

from sandbox import envfile
from sandbox.manager.chaos import ACTIONS, apply
from sandbox.registry.client import Registry


def main() -> None:
    from sandbox.manager.providers.apple_container import AppleContainerProvider

    envfile.load()
    if len(sys.argv) != 3 or sys.argv[1] not in ACTIONS:
        sys.exit("usage: chaos {kill|stop|delete} <vm_id>")
    action, vm_id = sys.argv[1], sys.argv[2]
    provider = AppleContainerProvider(image=os.environ.get("SANDBOX_VM_IMAGE", "sandbox-vm:dev"))
    apply(provider, Registry.from_env(), action, vm_id)
    print(f"{action} {vm_id}")


if __name__ == "__main__":
    main()
