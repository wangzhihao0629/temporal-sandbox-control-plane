"""Chaos CLI: break a VM the way a host failure would.

Usage: uv run python -m sandbox.manager.chaos {kill|stop|delete} <vm_id>
kill is a crash, stop is a drain (SIGTERM), delete removes the container
without warning. Each is recorded as an event with actor `chaos`.
Production: the equivalents are a host loss, an ASG terminate hook, and
TerminateInstances.
"""

import os
import sys

from sandbox import envfile
from sandbox.manager.providers.apple_container import AppleContainerProvider
from sandbox.registry.client import Registry


def main() -> None:
    envfile.load()
    if len(sys.argv) != 3 or sys.argv[1] not in ("kill", "stop", "delete"):
        sys.exit("usage: chaos {kill|stop|delete} <vm_id>")
    action, vm_id = sys.argv[1], sys.argv[2]
    provider = AppleContainerProvider(image=os.environ.get("SANDBOX_VM_IMAGE", "sandbox-vm:dev"))
    {"kill": provider.kill, "stop": provider.stop, "delete": provider.terminate}[action](vm_id)
    Registry.from_env().emit(
        "chaos", "chaos", f"{action} {vm_id}", vm_id=vm_id, details={"action": action}
    )
    print(f"{action} {vm_id}")


if __name__ == "__main__":
    main()
