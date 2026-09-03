"""Temporal sandbox demo.

What: a fully local model of running coding agents on VMs that Temporal
orchestrates but does not run on.
Why: to make the split between orchestration and execution readable, runnable,
and breakable on a laptop.
Production: the same packages become the sandbox-manager worker, the VM agent
installed in the AMI, and the client library the orchestrator imports.
"""

__version__ = "0.1.0"
