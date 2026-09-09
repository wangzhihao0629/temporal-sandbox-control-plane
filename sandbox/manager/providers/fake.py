"""A provider whose VMs are in-process agents.

What: launch starts an `InProcessVm` on the event loop; terminate stops it;
kill crashes it, which initiates no registry write; stop drains it; list and describe
report what is still running.
Why: the reconciler's whole loop, top-up from zero, dead-VM write-off, orphan
release, scale-in, runs against a real Temporal dev server and the real
`AgentRuntime` in a test that takes seconds, with no containers.
Production: never used. The EC2 provider takes this seat.

Threading: the manager runs providers from sync activities in a thread pool, so
every method here blocks on the loop with `run_coroutine_threadsafe`. Calling
them from the loop's own thread would deadlock; tests use `asyncio.to_thread`.
"""

import asyncio
import logging
from pathlib import Path

from sandbox.manager.providers.base import LaunchSpec, ProviderInstance
from sandbox.testing.inprocess_vm import InProcessVm

logger = logging.getLogger(__name__)


class FakeProvider:
    def __init__(self, loop, client, registry, store, root: Path, pool: str = "demo") -> None:
        self.loop = loop
        self.client = client
        self.registry = registry
        self.store = store
        self.root = Path(root)
        self.pool = pool
        self.vms: dict[str, InProcessVm] = {}
        self.launched: list[str] = []
        self.terminated: list[str] = []

    def _run(self, coro, timeout: float = 60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def _call(self, coro, what: str, provider_ref: str) -> None:
        """Run a teardown coroutine. The instance is already gone from `self.vms`.

        A real provider's terminate is fire and forget: the instance does not
        come back because the API call failed. Re-inserting it would leave the
        reconciler chasing a VM nobody can stop, so a failure is logged and the
        bookkeeping stands.
        """
        try:
            self._run(coro)
        except Exception:
            logger.warning("%s of %s failed", what, provider_ref, exc_info=True)

    def launch(self, vm_id: str, spec: LaunchSpec, env: dict[str, str]) -> str:
        # Registered before it boots: a start that dies halfway has already
        # written a registry row and may hold a worker, so the caller must be
        # able to see it. The failure path takes it back out again.
        vm = InProcessVm(
            self.client, self.registry, self.store, self.root, vm_id=vm_id, pool=self.pool
        )
        self.vms[vm_id] = vm
        try:
            self._run(vm.start())
        except BaseException:
            self.vms.pop(vm_id, None)
            try:
                self._run(vm.stop())
            except Exception:
                logger.warning("cleanup after failed launch of %s failed", vm_id, exc_info=True)
            raise
        self.launched.append(vm_id)
        return vm_id

    def terminate(self, provider_ref: str) -> None:
        self.terminated.append(provider_ref)
        vm = self.vms.pop(provider_ref, None)
        if vm is not None:
            self._call(vm.stop(), "terminate", provider_ref)

    def list(self) -> list[ProviderInstance]:
        # A snapshot: terminate and kill mutate self.vms from other threads.
        return [self._instance(vm) for vm in list(self.vms.values())]

    def describe(self, provider_ref: str) -> ProviderInstance | None:
        vm = self.vms.get(provider_ref)
        return self._instance(vm) if vm else None

    def kill(self, provider_ref: str) -> None:
        vm = self.vms.pop(provider_ref, None)
        if vm is not None:
            self._call(vm.runtime.crash(), "kill", provider_ref)

    def stop(self, provider_ref: str) -> None:
        vm = self.vms.pop(provider_ref, None)
        if vm is not None:
            self._call(vm.runtime.drain(), "stop", provider_ref)

    @staticmethod
    def _instance(vm: InProcessVm) -> ProviderInstance:
        return ProviderInstance(
            provider_ref=vm.vm_id,
            vm_id=vm.vm_id,
            state="running",
            created_at=vm.started_at,
            address="",
            gateway="",
        )
