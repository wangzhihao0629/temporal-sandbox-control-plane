"""Provider protocol and value types."""

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
