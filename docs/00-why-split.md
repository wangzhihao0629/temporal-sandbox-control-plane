# 00 · Why split the VM from the workflow

After this chapter you can explain, to someone who has only ever run a coding
agent next to its Temporal worker, why this repository puts a network boundary
between them, what a VM is allowed to promise a workflow, and which of two
competing designs this project picked and why.

## The problem today

Spec §1 names the starting point plainly: production coding agents run today with the Temporal worker on the same EC2 host
that executes the agent. The worker process, the Claude Code subprocess, and
the git checkout share one process tree. Nothing enforces a boundary between
"the code that decides what happens next" and "the code that happens to be
running the agent right now" — they are the same process, on the same box.

Three consequences follow directly from that coupling, and none of them are
about the agent being slow or wrong:

- **A host dies, and the workflow's activity dies with it.** There is no
  independent thing left running that could report the failure cleanly or hand
  the work to another machine; the workflow discovers the loss the same way it
  would discover a bug.
- **Scaling the fleet means scaling workers.** Because the worker and the
  agent's execution environment are one process tree, adding capacity for agent
  sessions means adding capacity for Temporal workers, whether or not the
  workflow logic itself needs more of anything.
- **The workflow knows about machines.** Deploying new orchestration code means
  rolling the fleet the agents run on, and worker secrets end up in the same
  environment the agent's subprocess inherits, because there is no boundary to
  put them behind.

## What a VM should be to a workflow

The fix this project builds is not a faster or more reliable VM. It is a smaller
one, in the sense that matters: a VM should look to a workflow like a **leased
resource with a small, versioned interface**, not like a place the workflow's
own process happens to live.

`sandbox/contract/names.py` is where that interface is named, and the names
matter more than they look like they should — a typo here is a silent hang on
some queue, not an error the workflow can catch. Every operation the wire
speaks is built by `activity_name(op)`, which turns `"acquire"` into
`sandbox.v1.acquire` by prefixing the contract's major version
(`CONTRACT_MAJOR = 1`, from `sandbox/contract/version.py`). The full set:

```
sandbox.v1.acquire          sandbox.v1.exec_cancel
sandbox.v1.release          sandbox.v1.put_file
sandbox.v1.exec_start       sandbox.v1.get_file
sandbox.v1.exec_wait        sandbox.v1.ensure_artifact
                             sandbox.v1.describe
```

`acquire` and `release` run on the manager's queue and answer one question:
which VM, if any, is this workflow allowed to touch right now. The other seven
— `VM_OPERATIONS` in `names.py` — run directly on that VM's own queue once a
lease names it, and they are exactly the four families the demo promises:
**exec** (`exec_start`, `exec_wait`, `exec_cancel`), **files** (`put_file`,
`get_file`), **artifacts** (`ensure_artifact`), and **describe**. A workflow
that only ever calls these nine names never needs to know what a VM is made of,
only that one exists somewhere behind a queue name.

## Two designs, one chosen

Spec §2 describes the fork this design sits on. One option keeps the agent loop
— Claude Code's or Codex's own loop, its skills, hooks, MCP client, and
compaction — running on the VM, with Temporal driving it through the small,
agent-agnostic primitives above. A turn, from the workflow's side, is just a
long `exec` job.

The other option is the Cursor-style design this project's inspiration describes
elsewhere in its own build: move the loop into Temporal itself and dispatch
every tool call the agent makes as its own activity. That buys per-tool-call
durability — a crash mid-tool-call resumes at the tool call, not at the start
of the turn — but it means owning an agent harness inside the workflow, one
that has to track every tool Claude Code or Codex might ever add.

Spec §17's decision table records the choice in one line: the agent loop stays
on the VM because it **avoids owning an agent harness**. Spec §2 gives the
fuller reasoning: doing it the other way is a strict superset of this design at
the VM layer — if per-tool-call durability is ever wanted, it becomes a second
caller of the same primitives this contract already exposes, not a reason to
redesign them.

## What the demo proves

Four properties, each demonstrated by running code rather than asserted in
prose:

**Leases outlive workers.** The manager that hands out leases is stateless
between calls — `sandbox/manager/worker.py`'s docstring calls this out
directly: it runs as a separate process from the orchestrator precisely so a
manager outage never stalls a turn already in flight, because execution goes
straight to the VM's own queue and never touches the manager again until
release. A lease is a row in the registry, not a variable in a worker's memory.

**A dead VM is a retry, not a failure.** `CodingSessionDemoWorkflow`'s own
docstring, in `sandbox/orchestrator/workflows.py`, says it directly: losing the
VM mid-session "costs the interrupted turn" — the next lease clones the session
bundle the runner saved after the previous turn, and the turn counter picks up
where it left off, because that counter lives in the object store's
`session.json`, not in the lost VM's memory. The workflow catches `LeaseLost`
and tries again, up to `max_lease_attempts` times, instead of failing outright.

**The fleet heals itself.** A scheduled reconciler —
`sandbox/manager/reconciler.py` — runs five steps every pass: health, leases,
capacity, requests, and a fleet sample. It reaps VMs a provider reports as
truly gone, releases leases whose owning workflow vanished, and launches
replacements to hold a pool at its policy's floor, with no human deciding any
of that in the moment.

**Everything is watchable.** The status dashboard and Temporal's own UI both
show real signals, not a summary invented for the demo: activities literally
named `sandbox.v1.exec_start` and the rest appear on a queue named after the VM
that ran them, and the dashboard is a set of joins over the same registry,
provider, and object store the workflow and the VM agent actually use.

## Read the code

- `sandbox/contract/names.py` — the nine operation names, and how
  `activity_name` turns an op into `sandbox.v1.<op>`.
- `sandbox/contract/version.py` — `CONTRACT_MAJOR`, the number that becomes the
  `v1` every activity name carries.
- `sandbox/orchestrator/workflows.py` — `CodingSessionDemoWorkflow`'s docstring,
  for the exact language on what losing a VM costs.
- `sandbox/manager/reconciler.py` — the module docstring's five reconciler steps
  and why a "stopped" instance is not treated as alive.
- `docs/superpowers/specs/2026-09-03-vm-sandbox-control-plane-design.md` — §1
  for the diagnosis, §2 for where the loop lives, §17 for the rejected
  alternative.

## Where this maps in production

Every workflow class in this repository names its production counterpart in its
own docstring, and this chapter's fork is no exception:
`sandbox/orchestrator/workflows.py` says outright that the production coding-agent workflows are the real callers a production version of this
contract would serve. The contract itself carries no production-specific code
to swap out — `sandbox/contract/names.py` says its production behavior is
"identical" — because the whole point of a small, versioned interface is that
the workflow-facing side does not change when the VM underneath it changes from
an Apple `container` on a laptop to an EC2 instance in a fleet.

## Try it

Nothing to run yet.

Next: [01 · The architecture in one picture](01-architecture.md)
