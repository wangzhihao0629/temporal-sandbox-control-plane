"""Chaos: break a VM the way a host failure would.

What: kill is a crash, stop is a drain (SIGTERM), delete removes the
container without warning. Each is recorded as an event with actor `chaos`.
Used by `make chaos-*` (sandbox/cli/chaos.py) and by the dashboard's chaos
endpoint, so both break a VM the same way and leave the same record.
Why: the reconciler's stale-heartbeat and missing-instance paths, and the VM
agent's drain handling, only prove themselves against a real failure — kill
exercises the first, delete the second, stop the third.
Production: the equivalents are a host loss, an ASG terminate hook, and
TerminateInstances.
"""

from sandbox.registry.client import Registry

ACTIONS = ("kill", "stop", "delete")


def apply(provider, registry: Registry, action: str, vm_id: str) -> dict:
    """Break `vm_id` the way `action` says and record who did it."""
    if action not in ACTIONS:
        raise ValueError(f"unknown chaos action {action!r}; expected one of {ACTIONS}")
    {"kill": provider.kill, "stop": provider.stop, "delete": provider.terminate}[action](vm_id)
    return registry.emit(
        "chaos", "chaos", f"{action} {vm_id}", vm_id=vm_id, details={"action": action}
    )
