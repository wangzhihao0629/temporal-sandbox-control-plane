"""A provider whose VMs are in-process agents.

What: launch starts an `InProcessVm` on the event loop; terminate stops it and
forgets it; kill crashes it, which initiates no registry write; stop drains it;
list and describe report both the running VMs and the ones that were killed or
drained, as `running` and `stopped`.
Why: the reconciler's whole loop, top-up from zero, dead-VM write-off, orphan
release, scale-in, runs against a real Temporal dev server and the real
`AgentRuntime` in a test that takes seconds, with no containers.
Production: never used. The EC2 provider takes this seat. A killed instance
lingers in `stopped` for the same reason a crashed EC2 instance keeps answering
DescribeInstances: only a terminate removes it, and the reconciler has to be
able to tell "stopped" from "gone".

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
        self.stopped: dict[str, InProcessVm] = {}
        self.launched: list[str] = []
        self.terminated: list[str] = []

    def _run(self, coro, timeout: float = 60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def _call(self, coro, what: str, provider_ref: str) -> None:
        """Run a teardown coroutine. The bookkeeping has already moved on.

        A real provider's teardown is fire and forget: the instance does not go
        back to running because the API call failed. Putting it back would leave
        the reconciler chasing a VM nobody can stop, so a failure is logged and
        the bookkeeping stands.
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
        vm = self.vms.pop(provider_ref, None) or self.stopped.pop(provider_ref, None)
        if vm is not None:
            self._call(vm.stop(), "terminate", provider_ref)

    def list(self) -> list[ProviderInstance]:
        # A snapshot: terminate, kill, and stop mutate both dicts from other threads.
        running = [self._instance(vm, "running") for vm in list(self.vms.values())]
        return running + [self._instance(vm, "stopped") for vm in list(self.stopped.values())]

    def describe(self, provider_ref: str) -> ProviderInstance | None:
        vm = self.vms.get(provider_ref)
        if vm is not None:
            return self._instance(vm, "running")
        vm = self.stopped.get(provider_ref)
        return self._instance(vm, "stopped") if vm is not None else None

    def kill(self, provider_ref: str) -> None:
        self._park(provider_ref, "kill", lambda vm: vm.runtime.crash())

    def stop(self, provider_ref: str) -> None:
        self._park(provider_ref, "stop", lambda vm: vm.runtime.drain())

    def _park(self, provider_ref: str, what: str, teardown) -> None:
        """Crash or drain the VM, and leave the instance behind as stopped."""
        vm = self.vms.pop(provider_ref, None)
        if vm is None:
            return
        self.stopped[provider_ref] = vm
        self._call(teardown(vm), what, provider_ref)

    @staticmethod
    def _instance(vm: InProcessVm, state: str) -> ProviderInstance:
        return ProviderInstance(
            provider_ref=vm.vm_id,
            vm_id=vm.vm_id,
            state=state,
            created_at=vm.started_at,
            address="",
            gateway="",
        )
