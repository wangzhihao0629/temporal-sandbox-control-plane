# temporal-sandbox-demo

An educational, fully local demo of splitting a coding agent's workflow from
its VM: Temporal drives a leased VM through a small contract instead of
sharing a process tree with it. Everything runs on one laptop: a Temporal dev
server, moto standing in for DynamoDB and S3, Apple `container` VMs, and a
dashboard.

## Run it

```
make bootstrap   # uv sync; checks for the temporal and container CLIs
make up          # container system, Temporal dev server, moto, .env
make image       # builds the sandbox-vm:dev image
make artifact    # packages and uploads the turn runner
make workers     # starts the manager, orchestrator, and status API
make demo        # runs a smoke check, then one coding session
```

Open `http://localhost:8600` for the dashboard, `http://localhost:8233` for
the Temporal UI.

## Read it

- [00 · Why split the VM from the workflow](docs/00-why-split.md) — the case for the split.
- [01 · The architecture in one picture](docs/01-architecture.md) — every component, one session.
- [02 · The contract: what a workflow may ask a VM](docs/02-the-contract.md) — the operations.
- [03 · Leases, the registry, and the reconciler](docs/03-lease-lifecycle.md) — a VM's states.
- [04 · Inside a VM: the agent that runs jobs](docs/04-vm-agent.md) — boot, jobs, heartbeat, drain.
- [05 · The provider: where VMs come from](docs/05-provider.md) — `container` standing in for EC2.
- [06 · A coding session: the orchestrator and the fake agent](docs/06-orchestrator-and-fake-agent.md) — the fix loop.
- [07 · Watching it: the status API and dashboard](docs/07-dashboard.md) — one page, built from joins.
- [08 · Running the demo](docs/08-running-the-demo.md) — up, through, and back down.
- [09 · From demo to production](docs/09-from-demo-to-production.md) — what changes.

Design spec and plans this course was built from: `docs/superpowers/`.
