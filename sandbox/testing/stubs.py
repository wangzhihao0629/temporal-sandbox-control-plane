"""A provider that records calls and owns no machines.

What: `StubProvider` for tests of code that needs a provider but never launches.
Why: `ManagerActivities` requires a provider so a destroy can never silently
skip termination; tests that only exercise acquire and release need something
to hand it.
Production: not deployed.
"""

from sandbox.manager.providers.base import LaunchSpec, ProviderInstance


class StubProvider:
    def __init__(self) -> None:
        self.terminated: list[str] = []
        self.killed: list[str] = []
        self.stopped: list[str] = []

    def launch(self, vm_id: str, spec: LaunchSpec, env: dict[str, str]) -> str:
        raise NotImplementedError("StubProvider does not launch")

    def terminate(self, provider_ref: str) -> None:
        self.terminated.append(provider_ref)

    def list(self) -> list[ProviderInstance]:
        return []

    def describe(self, provider_ref: str) -> ProviderInstance | None:
        return None

    def kill(self, provider_ref: str) -> None:
        self.killed.append(provider_ref)

    def stop(self, provider_ref: str) -> None:
        self.stopped.append(provider_ref)
