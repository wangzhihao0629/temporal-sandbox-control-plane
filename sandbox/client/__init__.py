"""The sandbox client: the only thing a workflow imports.

What: `Sandbox.lease()` and the `Lease` it yields, wrapping the manager and VM
activities with the right task queues, timeouts, retry policies, and error
translation.
Why: workflow authors should write `await vm.exec(...)` and catch `LeaseLost`,
not reason about per-VM queues and Temporal failure chains. Everything here is
deterministic and safe inside the workflow sandbox.
Production: the same package, imported by the orchestrator worker.
"""

from sandbox.client.sandbox import Lease, Sandbox
from sandbox.client.timeouts import Timeouts

__all__ = ["Lease", "Sandbox", "Timeouts"]
