"""Apple `container` as the pool provider.

What: launch, terminate, list, describe, kill, stop, by shelling out to the
`container` CLI and parsing its JSON.
Why: on macOS 26 each container is its own lightweight VM, so it stands in for
an EC2 instance with a Docker-like workflow. The VM id doubles as the container
name, so the manager knows the id before the VM registers itself.
Production: replaced by an EC2 provider over RunInstances, TerminateInstances,
DescribeInstances, and ASG instance protection. Nothing above this module
changes.
"""

import json
import subprocess

from sandbox.manager.providers.base import LaunchSpec, ProviderInstance

NAME_PREFIX = "sbx-"


class ProviderError(RuntimeError):
    pass


def run_cli(args: list[str]) -> str:
    proc = subprocess.run(["container", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProviderError(f"container {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def parse_instance(raw: dict) -> ProviderInstance:
    cfg = raw.get("configuration", {}) or {}
    status = raw.get("status", {}) or {}
    networks = status.get("networks") or []
    first = networks[0] if networks else {}
    return ProviderInstance(
        provider_ref=cfg.get("id", ""),
        vm_id=cfg.get("id", ""),
        state=status.get("state", "unknown"),
        created_at=cfg.get("creationDate", ""),
        address=first.get("ipv4Address", "").split("/")[0],
        gateway=first.get("ipv4Gateway", ""),
    )


class AppleContainerProvider:
    def __init__(self, image: str, runner=run_cli, name_prefix: str = NAME_PREFIX) -> None:
        self.image = image
        self.runner = runner
        self.name_prefix = name_prefix

    def launch(self, vm_id: str, spec: LaunchSpec, env: dict[str, str]) -> str:
        args = [
            "run",
            "--detach",
            "--init",
            "--name",
            vm_id,
            "--cpus",
            str(spec.cpus),
            "--memory",
            spec.memory,
        ]
        for key, value in env.items():
            args += ["--env", f"{key}={value}"]
        args.append(spec.image or self.image)
        self.runner(args)
        return vm_id

    def terminate(self, provider_ref: str) -> None:
        try:
            self.runner(["delete", "--force", provider_ref])
        except ProviderError:
            if self.describe(provider_ref) is not None:
                raise

    def kill(self, provider_ref: str) -> None:
        self.runner(["kill", "--signal", "KILL", provider_ref])

    def stop(self, provider_ref: str) -> None:
        self.runner(["stop", "--time", "20", provider_ref])

    def list_all(self) -> list[ProviderInstance]:
        raw = self.runner(["ls", "--all", "--format", "json"]) or "[]"
        return [parse_instance(item) for item in json.loads(raw)]

    def list(self) -> list[ProviderInstance]:
        return [i for i in self.list_all() if i.vm_id.startswith(self.name_prefix)]

    def describe(self, provider_ref: str) -> ProviderInstance | None:
        try:
            raw = self.runner(["inspect", provider_ref])
        except ProviderError:
            return None
        items = json.loads(raw or "[]")
        return parse_instance(items[0]) if items else None

    def gateway(self) -> str | None:
        for inst in self.list_all():
            if inst.gateway:
                return inst.gateway
        return None
