"""Provider protocol and value types.

What: the PoolProvider Protocol plus the two values every provider deals in, a
LaunchSpec going in and a ProviderInstance coming back.
Why: the manager's lifecycle logic must not know which compute backs a VM. Every
backend difference — an Apple container, an EC2 instance, a hosted sandbox —
stops at this interface, so the manager keeps one implementation of leasing.
Production: an EC2 provider over the ASG and EC2 APIs.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LaunchSpec:
    image: str
    cpus: int = 2
    memory: str = "2048M"


@dataclass(frozen=True)
class ProviderInstance:
    provider_ref: str
    vm_id: str
    state: str
    created_at: str
    address: str
    gateway: str


class PoolProvider(Protocol):
    def launch(self, vm_id: str, spec: LaunchSpec, env: dict[str, str]) -> str: ...

    def terminate(self, provider_ref: str) -> None: ...

    def list(self) -> list[ProviderInstance]: ...

    def describe(self, provider_ref: str) -> ProviderInstance | None: ...

    def kill(self, provider_ref: str) -> None: ...

    def stop(self, provider_ref: str) -> None: ...
