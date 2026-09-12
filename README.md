# Sandbox Control Plane

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![macOS Apple Silicon](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-lightgrey.svg)

An educational, fully local demo of splitting a coding agent's workflow from
the VM it runs on. A [Temporal](https://temporal.io) workflow leases a VM
through a small versioned contract, drives it through a coding session, and
hands it back — the workflow itself never runs on the machine it's directing.
Everything runs on one laptop: a Temporal dev server, [moto](https://github.com/getmoto/moto)
standing in for DynamoDB and S3, Apple `container` VMs, and a live dashboard.

## What it demonstrates

- **Leases outlive workers.** Kill the orchestrator mid-turn — the in-flight
  step is retried and reattaches to the job record still on the VM's disk. No
  rerun.
- **A dead VM is a retry, not a failure.** Kill the VM under a running
  session and it finishes on a second one, picking up from the last saved
  turn.
- **The fleet heals itself.** A reconciler tops the pool up from nothing,
  writes off dead VMs, releases orphaned leases, and scales out under demand
  and back in after a cooldown — all through conditional writes on one
  DynamoDB row, so two managers can never win the same lease.
- **Everything is watchable.** A dashboard joins the registry, the VM
  provider, Temporal, and S3 into one page with a live event feed; the
  Temporal UI shows every activity on its queue.

See [`docs/sandbox-control-plane.html`](docs/sandbox-control-plane.html) for
a full diagram of the components, one coding session traced activity by
activity, and the VM lifecycle state machine — open it directly in a browser.

## Requirements

- **macOS on Apple Silicon** — the VM layer uses Apple's `container` CLI
  (Homebrew: `brew install container`), which is what actually requires the
  silicon and the OS version.
- The [`temporal`](https://docs.temporal.io/cli) CLI.
- [`uv`](https://docs.astral.sh/uv/) and Python 3.12+.

`make bootstrap` checks for the first two and fails with an install hint if
either is missing.

## Quick start

```sh
make bootstrap   # uv sync; checks for the temporal and container CLIs
make up          # container system, Temporal dev server, moto, .env
make image       # builds the sandbox-vm:dev image (slow the first time)
make artifact    # packages and uploads the turn runner
make workers     # starts the manager, orchestrator, and status API
make demo        # runs a smoke check, then one coding session
```

Open **http://localhost:8600** for the dashboard and
**http://localhost:8233** for the Temporal UI while it runs.

Tear down with `make down` (after stopping `make workers` with Ctrl-C).

## Try it further

Beyond the one-shot demo, there are targeted drills for each failure mode:

```sh
make session SCENARIO=multiply-with-bug   # a session with a fix loop
make chaos-kill VM=<vm-id>                # kill a leased VM mid-turn
make chaos-stop VM=<vm-id>                # drain a leased VM (SIGTERM)
make hold SECONDS=120                     # hold a lease, then orphan it
make show                                 # raw registry/fleet snapshot
```

[Chapter 08](docs/08-running-the-demo.md) walks through all twelve demo
steps — cold start, smoke, session, orchestrator/manager restarts, VM death,
orphaned leases, capacity, drain, giving up, and scale-in — with the exact
command, what to watch for, and the code path behind each one.

## Documentation

A numbered course, each chapter building on the last:

- [00 · Why split the VM from the workflow](docs/00-why-split.md)
- [01 · The architecture in one picture](docs/01-architecture.md)
- [02 · The contract: what a workflow may ask a VM](docs/02-the-contract.md)
- [03 · Leases, the registry, and the reconciler](docs/03-lease-lifecycle.md)
- [04 · Inside a VM: the agent that runs jobs](docs/04-vm-agent.md)
- [05 · The provider: where VMs come from](docs/05-provider.md)
- [06 · A coding session: the orchestrator and the fake agent](docs/06-orchestrator-and-fake-agent.md)
- [07 · Watching it: the status API and dashboard](docs/07-dashboard.md)
- [08 · Running the demo](docs/08-running-the-demo.md)
- [09 · From demo to production](docs/09-from-demo-to-production.md)

## Testing

```sh
make test   # uv run pytest -q
make lint   # ruff
```

These run against the pure/unit and moto-backed integration suites — no VM
or Temporal server required.

## Project layout

```
sandbox/         the system itself
  contract/      the versioned wire contract (types, activity names, errors)
  registry/      the DynamoDB-backed VM/lease/job registry
  manager/       acquire/release activities, the reconciler, chaos tooling
  vm_agent/      what runs inside a VM: boot, exec, heartbeat, drain
  orchestrator/  the demo workflows and the fake coding agent
  client/        the workflow-facing sandbox client
  status/        the read API and dashboard behind it
images/vm/       the VM image: Dockerfile, entrypoint, seed repository
scripts/         bring-up/tear-down and image-build scripts
tests/           unit, integration, and status-API test suites
docs/            the numbered course and the architecture diagram
```

## License

[MIT](LICENSE)
