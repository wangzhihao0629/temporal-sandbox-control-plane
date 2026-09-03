"""The VM agent's heartbeat loop.

What: every few seconds, bump the registry heartbeat and act on the row's state.
Why: the registry is how the manager talks to the VM without a direct call.
A row flipped to `recycling` means the lease ended; the VM wipes itself and
flips back to `idle`. A missing row means the reconciler declared this VM dead,
and the process exits so the container stops.
Production: identical, at 30 seconds instead of 5.
"""

import asyncio
from collections.abc import Callable

from sandbox.registry.client import Registry
from sandbox.vm_agent.config import AgentConfig


class HeartbeatLoop:
    def __init__(
        self,
        cfg: AgentConfig,
        registry: Registry,
        wipe_fn: Callable[[], None],
        on_row_missing: Callable[[], None],
    ) -> None:
        self.cfg = cfg
        self.registry = registry
        self.wipe_fn = wipe_fn
        self.on_row_missing = on_row_missing

    async def tick(self) -> str | None:
        state = await asyncio.to_thread(self.registry.heartbeat, self.cfg.vm_id)
        if state is None:
            self.on_row_missing()
            return None
        if state == "recycling":
            await asyncio.to_thread(self.wipe_fn)
            await asyncio.to_thread(
                self.registry.set_state,
                self.cfg.vm_id,
                "idle",
                expect="recycling",
                reason="wiped",
            )
            await asyncio.to_thread(
                self.registry.emit, "wipe", "vm-agent", "workspace wiped", self.cfg.vm_id
            )
            return "idle"
        return state

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            state = await self.tick()
            if state is None:
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.heartbeat_seconds)
            except TimeoutError:
                pass
