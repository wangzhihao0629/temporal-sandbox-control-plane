"""Test support shipped with the package.

What: an in-process VM agent over temp directories.
Why: the reconciler's FakeProvider and the integration tests both need a VM
that boots in milliseconds and dies on command; keeping one implementation in
the package means the provider used in tests is the real `AgentRuntime`, not a
stub of it.
Production: not deployed. The VM image runs `sandbox.vm_agent.worker` instead.
"""
