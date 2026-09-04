"""The orchestrator: workflows that use VMs.

What: demo workflows and the worker that hosts them.
Why: this is the stand-in for the production agent orchestrator. It imports
only `sandbox.client` and `sandbox.contract`; it never sees the registry, the
provider, or a VM queue name.
Production: the `agent-orchestrator` worker on EKS running the real agent workflows.
"""
