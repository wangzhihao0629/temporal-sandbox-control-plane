"""Launch one VM by hand.

Usage: uv run python -m sandbox.manager.launch_vm [pool]

Plan 2's reconciler makes this unnecessary; until then it is how the demo gets
capacity.
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
