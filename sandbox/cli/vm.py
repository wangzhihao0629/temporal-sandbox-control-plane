"""Launch one VM by hand.

What: pick a vm_id, ask the Apple `container` provider for a machine, print the id.
Why: the demo needs capacity before there is anything to reconcile. Everything a
VM needs to find its way home — Temporal, the registry, the object store — is
computed here rather than baked into the image, so the same image boots against
any stack.
Production: the reconciler's scale-out decides this, and the provider is an ASG.

Usage: uv run python -m sandbox.cli.vm [pool]
"""

import sys

from sandbox import envfile
from sandbox.manager.launch import launch_spec, new_vm_id, vm_environment
from sandbox.manager.providers.apple_container import AppleContainerProvider


def main() -> None:
    envfile.load()
    pool = sys.argv[1] if len(sys.argv) > 1 else "demo"
    spec = launch_spec()
    provider = AppleContainerProvider(image=spec.image)
    vm_id = new_vm_id()
    provider.launch(vm_id, spec, vm_environment(vm_id, pool))
    print(vm_id)


if __name__ == "__main__":
    main()
