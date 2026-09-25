# 01 · The architecture in one picture

After this chapter you can point at any box in this system, name the package
that implements it, and trace one coding session from the moment the
orchestrator asks for a VM to the moment that VM goes back to idle.

## The picture

Spec §3 draws the target architecture; this is the same shape, redrawn for what
actually runs on a laptop today. Three host processes — `manager`,
`orchestrator`, `status` — are started together by `make workers`, because that
is what the `Procfile` lists and what `honcho start` runs. The Temporal dev
server and `moto_server` are separate processes `make up` starts before any of
that.

```
host: temporal dev server            host: honcho (make workers)
┌───────────────────────┐            ┌────────────────────────────────┐
│ grpc 127.0.0.1:7233    │◀───────────│ orchestrator worker            │
│ ui  http://:8233       │            │   orchestrator-queue           │
├───────────────────────┤            │   SmokeWorkflow, HoldWorkflow,  │
│ moto_server :5050      │◀───────┐  │   CodingSessionDemoWorkflow     │
│   dynamodb + s3 APIs   │        │  ├────────────────────────────────┤
└───────────────────────┘        │  │ manager worker                  │
          ▲                       └──│   sandbox-manager-queue         │
          │ registry + object store  │   acquire, release, reconciler  │
          │                          ├────────────────────────────────┤
          │                          │ status API :8600                │
          └──────────────────────────│   joins registry + provider +   │
                                      │   Temporal + object store       │
                                      └────────────────────────────────┘
                                                    │ container CLI
                                                    ▼
                                      Apple `container` VMs, one per sbx-*
                                      ┌────────────────────────────────┐
                                      │ sbx-3f9c                        │
                                      │   vm_agent worker on            │
                                      │   sandbox-vm-sbx-3f9c           │
                                      │   register, heartbeat, exec,    │
                                      │   files, artifact cache, wipe   │
                                      └────────────────────────────────┘
```

The orchestrator worker only ever talks to Temporal, never to a machine directly
— `sandbox.client` is the only import standing between it and a queue name.
Execution activities skip the manager entirely once a lease exists: they run
straight on the VM's own queue.

## The components

**Contract** (`sandbox/contract/`) — frozen dataclasses, activity names, the
contract's major and minor version, and nothing else. Every other package
depends on it; it depends on nothing, and its production form is "a shared
wheel."

**Client** (`sandbox/client/`) — `Sandbox.lease()` and the `Lease` it yields.
Its own docstring calls it "the only thing a workflow imports": everything a
workflow does to a VM — `exec`, file transfer, artifact fetch, `describe` —
goes through typed methods here, with the right queue, timeout, and error
translation already attached.

**Manager, with the reconciler** (`sandbox/manager/`,
`sandbox/manager/reconciler.py`) — hosts the `acquire` and `release` activities
plus a scheduled `ReconcileWorkflow`. It runs as its own process so a manager
outage stalls neither a lease already granted nor a turn already running,
because both go straight to a VM's queue.

**Registry** (`sandbox/registry/`) — every read and write the manager, the VM
agent, and the dashboard make against the system's real state. Its own
docstring is blunt about why it exists: "conditional writes are the whole
point" — one VM cannot be claimed twice, and a release is idempotent on the
lease id.

**VM agent** (`sandbox/vm_agent/`) — registers a VM, heartbeats it, runs the
exec/file/artifact activities on that VM's own queue, wipes it, and drains it.
The same `AgentRuntime` object runs inside the image and inside this
repository's integration tests, so a test exercises the real boot sequence.

**Provider** (`sandbox/manager/providers/apple_container.py`) — launch,
terminate, list, describe, kill, and stop, by shelling out to the `container`
CLI and parsing its JSON. Every call carries its own timeout, because a wedged
daemon must not be able to hold a manager activity thread forever.

**Runner** (`sandbox/runner/`) — "the code the orchestrator ships to a VM," in
its own words: a CLI with nine subcommands, versioned by the artifact the
orchestrator asks for, so the orchestrator — not the VM image — decides which
runner runs.

**Orchestrator** (`sandbox/orchestrator/`) — the demo workflows themselves,
built only on `sandbox.client`, plus the two small activities that read a
runner envelope and publish a session summary without ever putting the object
store inside the workflow sandbox.

**Status** (`sandbox/status/`) — a FastAPI process that reads the registry, the
provider, Temporal, and the object store, and serves one page. Its own
docstring: "the registry is the system's real state, but a demo needs to be
watched, not queried."

## One session, end to end

A `CodingSessionDemoWorkflow` run, from the orchestrator's perspective:

1. The orchestrator worker, polling `orchestrator-queue`, starts the workflow.
   It builds a `Sandbox` and enters `sandbox.lease(spec)`, which calls
   `sandbox.v1.acquire` on `sandbox-manager-queue`.
2. The manager claims an idle registry row and returns a `SandboxLease` naming
   that VM's own queue — `sandbox-vm-<vm_id>`, built by `vm_task_queue()` in
   `sandbox/contract/names.py`.
3. Still inside the lease, the workflow calls `sandbox.v1.ensure_artifact` on
   that queue to fetch the runner artifact onto the VM, by URI and sha256.
4. Each step of the session — clone, a turn, lint, test, and eventually export —
   is its own `sandbox.v1.exec_start` / `sandbox.v1.exec_wait` pair on the same
   VM queue, built by `run_step` in `sandbox/orchestrator/steps.py`.
5. Turns repeat, feeding the previous test failure back in as the next turn's
   context, until the tests pass or `max_turns` is reached.
6. The workflow pulls the resulting patch back with `sandbox.v1.get_file`, then
   publishes a summary through the orchestrator's own
   `orchestrator.publish_summary` activity.
7. `lease()`'s `finally` calls `sandbox.v1.release` on `sandbox-manager-queue`.
   The manager flips the row to `recycling`; the VM agent notices on its next
   heartbeat, wipes the workspace, and flips itself back to `idle`.
8. If the lease is lost partway through, the workflow catches `LeaseLost` and
   retries the whole loop from step 1 — up to `max_lease_attempts` times —
   resuming from the session bundle the runner saved after the last turn that
   actually finished.

## What talks to what

| Component | Reads | Writes | Over what |
|---|---|---|---|
| Orchestrator workflow | a lease it was granted | exec, file, and artifact requests | Temporal activities on the manager queue and the VM's own queue |
| Manager | registry rows, provider inventory | registry rows (claim, release, state) | Temporal activities on `sandbox-manager-queue`; the `container` CLI |
| Reconciler | provider `list`, registry rows | registry rows, the event log, a fleet sample | a scheduled workflow on the manager queue |
| VM agent | its own registry row, its job files | registry (heartbeat, state), object store (logs, envelopes) | a Temporal worker on `sandbox-vm-<vm_id>`; the registry and S3 APIs |
| Runner | workspace files, the session bundle | the patch, step envelopes, `session.json` | the local filesystem and object store URIs the VM agent hands back |
| Status API | registry, provider, Temporal, object store | demo-only chaos and policy writes | HTTP and SSE to the dashboard page |

## Read the code

- `sandbox/contract/names.py` — the task queue constants and `vm_task_queue`,
  the source of every queue name this chapter quotes.
- `sandbox/orchestrator/workflows.py` — `CodingSessionDemoWorkflow.run`, the
  code behind the numbered flow above.
- `sandbox/client/sandbox.py` — `Sandbox.acquire`, `.release`, and `.lease()`,
  where each queue name is actually used in a `workflow.execute_activity` call.
- `sandbox/manager/reconciler.py` — the reconciler steps behind "the fleet heals
  itself."
- `sandbox/status/api.py` — the joins behind the dashboard's one page.
- `Procfile` — the three host processes `make workers` starts together.

## Where this maps in production

Spec §4's component table gives each piece here a named production successor:
the orchestrator worker becomes the orchestrator worker on EKS —
`sandbox-orchestrator` in its own docstring, the `agent-orchestrator` deploy in spec §4's
table — running the production agent; the sandbox manager worker becomes a `sandbox-manager`
worker on EKS; the registry's DynamoDB API, mocked locally by `moto_server`,
becomes real DynamoDB; the provider's Apple `container` calls become EC2 and
ASG APIs; the VM agent ships unchanged as the package an AMI installs; the
object store's local `moto_server` S3 becomes real S3; and the status API
becomes an internal dashboard app reading the same registry, provider, and Temporal
client. No box in the picture above is demo-only — every one has a named
production target already.

## Try it

Nothing to run yet; [chapter 08](08-running-the-demo.md) runs all of it.

Next: [02 · The contract: what a workflow may ask a VM](02-the-contract.md)
