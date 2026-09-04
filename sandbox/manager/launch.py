"""How a VM is launched.

What: the VM id scheme, the launch spec from the environment, and the
environment a VM boots with.
Why: shared by the manual launcher here and the reconciler in Plan 2, so both
paths produce identical VMs. The boot environment carries the VM's identity
and the host-reachable endpoints; the credentials in it are the VM's own
object-store identity, never a caller's.
Production: user_data written by the EC2 provider from the same function.
"""

import os
import secrets

from sandbox.manager.providers.base import LaunchSpec


def new_vm_id() -> str:
    return f"sbx-{secrets.token_hex(4)}"


def launch_spec(env=os.environ) -> LaunchSpec:
    return LaunchSpec(
        image=env.get("SANDBOX_VM_IMAGE", "sandbox-vm:dev"),
        cpus=int(env.get("SANDBOX_VM_CPUS", "2")),
        memory=env.get("SANDBOX_VM_MEMORY", "2048M"),
    )


def vm_environment(vm_id: str, pool: str, env=os.environ) -> dict[str, str]:
    return {
        "VM_ID": vm_id,
        "POOL": pool,
        "PROVIDER_REF": vm_id,
        "TEMPORAL_ADDRESS": env["VM_TEMPORAL_ADDRESS"],
        "TEMPORAL_NAMESPACE": env.get("TEMPORAL_NAMESPACE", "default"),
        "DYNAMODB_ENDPOINT": env["DYNAMODB_ENDPOINT"],
        "S3_ENDPOINT": env["S3_ENDPOINT"],
        "AWS_ACCESS_KEY_ID": env["AWS_ACCESS_KEY_ID"],
        "AWS_SECRET_ACCESS_KEY": env["AWS_SECRET_ACCESS_KEY"],
        "AWS_DEFAULT_REGION": env.get("AWS_DEFAULT_REGION", "us-east-1"),
        "AGENT_VERSION": env.get("SANDBOX_AGENT_VERSION", "0.1.0"),
        "SANDBOX_RUN_AS_USER": "agent",
        "SANDBOX_HEARTBEAT_SECONDS": env.get("SANDBOX_HEARTBEAT_SECONDS", "5"),
    }
