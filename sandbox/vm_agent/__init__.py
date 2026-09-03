"""The VM agent: the only long-lived process on a VM.

What: a Temporal worker on a per-VM task queue that starts jobs, reports on
them, moves files, caches artifacts, heartbeats to the registry, wipes between
leases, and drains on shutdown.
Why: it knows nothing about workflows or agents, so the orchestrator can change
daily while this stays put. Job records live on disk so a wait can reattach
after any restart on the other side.
Production: installed in the AMI as a pinned wheel and run as a system user.
Locally: the entrypoint of the VM image.
"""
