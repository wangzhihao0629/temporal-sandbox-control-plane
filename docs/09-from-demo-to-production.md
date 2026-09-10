# 09 · From demo to production

After this chapter you can point at any piece of this demo and say what changes
when it ships in a real repository, name the five phases that get it there, and
list every corner this demo cut on purpose so a reviewer never mistakes one of
them for an oversight.

## What changes and what does not

Spec §16's table, one sentence of reasoning added per row:

| Demo piece | Becomes | Repo | Why |
|---|---|---|---|
| Apple `container` provider | EC2 over ASG and EC2 APIs, protection via instance protection | agent-host-repo or the manager repo | `sandbox/manager/providers/base.py` is the seam precisely so a backend swap needs no change above it, and the `protected` registry flag the reconciler already honors maps straight onto real instance protection. |
| Host `moto_server` | real DynamoDB and S3, endpoints removed, IAM per writer | production Terraform | moto answers the same DynamoDB and S3 wire APIs `sandbox/registry/client.py` and `sandbox/objectstore.py` already call, so only the endpoint and the credential model change. |
| Fake agent and seed repo | a real `claude -p` turn against real repos; clone/lint/test/export become `ProductionCodingWorkflow`'s own steps | orchestration-repo | `sandbox/runner/agent.py`'s own docstring says the orchestrator does not care which agent ran — the loop and the step shape are unchanged. |
| Runner artifact, runner package only | a per-commit virtualenv built by CI | orchestration-repo | `sandbox/runner/package.py` already says why: the demo image supplies the dependencies, production supplies only the interpreter. |
| Host workers | the orchestrator worker on EKS (`sandbox-orchestrator` in its own docstring, `agent-orchestrator` in spec §4's table) and `sandbox-manager` on EKS via platform worker bootstrap | orchestration-repo | chapter 01's component table already names these as the orchestrator and manager workers' production successors. |
| VM agent in the image | the same package pinned in the Packer AMI, IMDS drain instead of `SIGTERM` | agent-host-repo | chapter 04's mapping already shows most of the package saying "Production: identical" in its own docstring; only the config source, the heartbeat interval, and the drain trigger actually move. |
| `sandbox.contract`, `sandbox.client` | one wheel both repos depend on | new | each module already claims to be production-identical, which is what makes shipping it as a shared dependency rather than duplicated code possible at all. |
| Status page | an internal dashboard app over the same API | dashboard-platform-repo | chapter 07's own mapping: nothing about the endpoint shapes or the join logic changes, only the identity behind the Temporal client and the provider. |
| `summary.json` as the result | a pull request | orchestration-repo | the orchestrator's own activities docstring calls the summary a stand-in for opening one: the same information, a different destination. |

## The phases in orchestration-repo

Spec §16 lists five phases, all gated behind one flag so the existing macOS path
keeps serving traffic until each phase is ready:

1. **Extract the runner CLI from `macos_dispatch`.** Moves its existing macOS turn
   logic into a CLI shaped like `sandbox/runner`. What proves it: that CLI runs
   the existing macOS path unchanged, with no VM involved yet.
2. **Ship the VM agent, registry, manager, and a second pool.** Moves this
   demo's own packages into that repository as a second, VM-backed pool running
   beside the macOS fleet. What proves it: a reconciler holding that pool at
   its floor — the same signal chapter 03's fleet sample already demonstrates —
   with no macOS session yet moved onto it.
3. **Split clone, push, and PR into exec activities; move the orchestrator to
   EKS.** Moves the turn loop off the macOS host and behind the same nine
   contract operations chapter 02 named. What proves it: a Temporal history
   showing `sandbox.v1.exec_start`/`exec_wait` pairs per step, the shape
   `CodingSessionDemoWorkflow` already produces, instead of one long
   macOS-resident step.
4. **Move the macOS path and agent platform onto the interface.** Moves every
   remaining caller onto `sandbox.client` so nothing but the contract talks to
   a VM. What proves it: both callers importing only `sandbox.client`, the same
   constraint chapter 01 states for this demo's own orchestrator.
5. **Snapshot and hibernate for persistent sessions.** Moves a session's state
   off "must finish on one VM" and onto the object store, the way
   `session.py`'s git bundle already does for a lost VM. What proves it: a
   session resuming on a new VM after a deliberate pause, not just after a
   crash.

## Known gaps in the demo

- **No continue-as-new.** A session here is a handful of turns in one workflow
  run. Spec §16's own paragraph on this names what a many-turn production
  session needs instead: `CodingSessionParams` carrying the current
  `SandboxLease` across a continue-as-new, and the reconciler's leases step
  following the workflow id, not the run id, which changes each time.
  `docs/superpowers/plans/2026-09-09-plan-3-coding-session.md` rules this out
  of the demo's scope.
- **No stale-writer fencing.** Spec §16's last paragraph names the window: a
  runner whose lease was just declared lost can still write `repo.bundle` and
  `session.json` for a few seconds before its own VM's heartbeat loop notices
  and wipes it. Production would close it with a lease id recorded per write,
  or the object store's own conditional put.
- **The capacity step's timeout assumes a pool max of five.**
  `sandbox/manager/reconcile.py`'s `_CAPACITY_STEP` sets a 16-minute
  start-to-close from `60 + 5 * 180` seconds — one `container ls` plus five
  launches — with the 5 written in, not read from `policy.max`. Raise `max`
  past five and a pass that fills the pool can outlive its activity, retry, and
  launch beside a `container run` still going. Plan 3's self-review parks
  deriving that budget from the policy.
- **Event keys are millisecond timestamps.** `sandbox/registry/client.py`'s
  `emit` builds `ts_ulid` from `now_iso()` (`sandbox/timeutil.py`'s `to_iso`,
  millisecond precision) plus a random eight-character suffix — sortable most
  of the time, but not a real ULID.
- **No monotonic event key.** The same random suffix means two events emitted in
  the same millisecond have no defined order between them —
  `tests/status/test_events.py`'s own comment says so, and spaces its emits to
  keep its ordering assertions meaningful.
- **Cost excludes turns on a lost VM.** `CodingSessionDemoWorkflow`'s running
  total only adds a turn's `fake_cost_usd` after that turn's exec step returns;
  a turn interrupted by `LeaseLost` contributes nothing to the reported cost,
  even if its steps already ran partway through it.
- **The polling cost notes.** The dashboard's own two-second refresh drives
  `provider.list()`, a `container ls` subprocess; a `provider_cache_seconds`
  cache in `sandbox/status/api.py` only keeps that from stacking calls on one
  laptop. Plan 4's Global Constraints name that as the reason the cache exists,
  not a real-fleet-scale polling plan.

## What to read next

The full design lives in
`docs/superpowers/specs/2026-09-03-vm-sandbox-control-plane-design.md` — this
chapter only paraphrases §16 and §17. The five plans that built this
repository, in order, are
`docs/superpowers/plans/2026-09-03-plan-1-execution-layer.md`,
`docs/superpowers/plans/2026-09-09-plan-2-reconciler.md`,
`docs/superpowers/plans/2026-09-09-plan-3-coding-session.md`,
`docs/superpowers/plans/2026-09-10-plan-4-dashboard.md`, and
`docs/superpowers/plans/2026-09-10-plan-5-docs.md`, the one this course came
from. The rulings the gaps above cite — continue-as-new, the capacity budget,
the provider cache — live in one of their Global Constraints or self-review
sections, in the same words a reviewer of this repository read before approving
the shortcut.

## Read the code

- `docs/superpowers/specs/2026-09-03-vm-sandbox-control-plane-design.md` — §16's
  table and phases, §17's rejected alternatives.
- `sandbox/orchestrator/workflows.py` — the cost accumulator's placement
  relative to the lease retry loop.
- `sandbox/registry/client.py`, `sandbox/timeutil.py` — `emit`, `ts_ulid`,
  `to_iso`.
- `sandbox/bootstrap.py`, `sandbox/manager/reconciler.py` — the seeded pool
  policy and the capacity step that spends it.
- `sandbox/status/api.py` — `provider_cache_seconds`.
- `tests/status/test_events.py` — the comment on same-millisecond ordering.
- The five plan files listed above, each read for its Global Constraints and any
  self-review notes.

## Where this maps in production

Every row in the table above already names its successor repo, so this chapter
adds no separate mapping of its own: its "production" is
orchestration-repo, agent-host-repo, dashboard-platform-repo, and
production Terraform, picking up the five phases above, behind spec
§16's flag.

## Try it

Nothing to run: this chapter is a map, not a drill. To see the boundary the
table above draws, compare a module's own docstring against its own
"Production:" line — `sandbox/contract/names.py`,
`sandbox/vm_agent/runtime.py`, and
`sandbox/manager/providers/apple_container.py` each say outright what changes
and what does not.

Back to the [index](../README.md)
