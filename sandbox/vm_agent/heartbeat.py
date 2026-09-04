"""The VM agent's heartbeat loop.

What: every few seconds, bump the registry heartbeat and act on the row's state.
Why: the registry is how the manager talks to the VM without a direct call.
A row flipped to `recycling` means the lease ended; the VM wipes itself and
flips back to `idle`. A row that is missing, `dead`, or `terminated` means the
reconciler has written this VM off, and the process exits so the container
stops instead of heartbeating a row nobody will ever claim again.
Production: identical, at 30 seconds instead of 5.
"""

import asyncio
import logging
from collections.abc import Callable

from sandbox.registry.client import Registry
from sandbox.vm_agent.config import AgentConfig

logger = logging.getLogger(__name__)


# States the reconciler writes when it has given up on a VM. Heartbeating past
# one of them keeps a machine alive that nothing will ever lease again.
WRITTEN_OFF = ("terminated", "dead")


class HeartbeatLoop:
    def __init__(
        self,
        cfg: AgentConfig,
        registry: Registry,
        wipe_fn: Callable[[], None],
        on_written_off: Callable[[], None],
    ) -> None:
        self.cfg = cfg
        self.registry = registry
        self.wipe_fn = wipe_fn
        self.on_written_off = on_written_off

    async def tick(self) -> str | None:
        state = await asyncio.to_thread(self.registry.heartbeat, self.cfg.vm_id)
        if state is None or state in WRITTEN_OFF:
            self.on_written_off()
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
            # A throttled or briefly unreachable registry must not kill the loop:
            # the VM would then stop heartbeating and the reconciler would declare
            # a healthy machine dead. Only a row that is gone or written off ends it.
            try:
                state = await self.tick()
            except Exception:
                logger.warning("heartbeat tick failed for %s", self.cfg.vm_id, exc_info=True)
            else:
                if state is None:
                    return
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.heartbeat_seconds)
            except TimeoutError:
                pass
