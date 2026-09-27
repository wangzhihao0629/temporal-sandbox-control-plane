"""The sandbox manager: the VM control plane.

What: acquire and release activities, pool policy, and chaos; the reconciler
that owns pool size and health is `reconciler/`, and where VMs come from is
`providers/`.
Why: workflows must never touch the registry or the provider. The manager is
the only writer of leases and the only caller of launch and terminate, so
inventory has one owner. It is stateless between calls; every fact lives in
the registry.
Production: the sandbox-manager worker on EKS with a cross-account role into
the VM account.
"""
