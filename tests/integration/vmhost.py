"""Re-export of the in-process VM harness, kept so existing test imports work."""

from sandbox.testing.inprocess_vm import InProcessVm

__all__ = ["InProcessVm"]
