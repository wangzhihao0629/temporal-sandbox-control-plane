"""VM agent entry point.

What: build the runtime from the environment, install SIGTERM as drain, run.
Why: this is PID 1's child in the VM image; `container stop` sends SIGTERM and
the agent turns that into a drain.
Production: the same entry point, with an IMDS watcher calling drain instead
of a signal.
"""

import asyncio
import logging
import signal

from temporalio.client import Client

from sandbox.objectstore import ObjectStore
from sandbox.registry.client import Registry
from sandbox.vm_agent.config import AgentConfig
from sandbox.vm_agent.runtime import AgentRuntime


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    cfg = AgentConfig.from_env()
    client = await Client.connect(cfg.temporal_address, namespace=cfg.temporal_namespace)
    runtime = AgentRuntime(cfg, client, Registry.from_env(), ObjectStore.from_env())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(runtime.drain()))
    logging.getLogger(__name__).info(
        "vm agent %s starting on %s", cfg.vm_id, cfg.temporal_address
    )
    await runtime.run_until_stopped()


if __name__ == "__main__":
    asyncio.run(main())
