"""The wire contract between workflows and VMs.

What: frozen, JSON-native dataclasses, versioned activity names, and error
types that both sides of the boundary import.
Why: the contract is the deliverable. Providers, stores, and agents are
swappable; this package is what stays stable.
Production: published as one wheel that the orchestrator repo and the VM image
build both depend on.
"""
