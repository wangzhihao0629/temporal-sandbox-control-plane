# 02 · The contract: what a workflow may ask a VM

After this chapter you can list the eleven operations a workflow may perform on a
VM, name the dataclass each one carries, explain why `acquire` never blocks and
`release` never gives up, and read the six error types a caller can catch
without knowing Temporal's own failure types.

## Principles

Chapter 00 named `sandbox/contract/names.py` and `activity_name(op)`, which
turns `"acquire"` into `sandbox.v1.acquire` by prefixing `CONTRACT_MAJOR = 1`.
This chapter covers the rest of that package: the types those names carry and
the client that calls them.

Spec §5.1 states five principles the code holds to:

- **Payload limits.** Files and logs never cross as bytes; they move as object
  store URIs, and results carry a tail plus a pointer, not the output.
- **Determinism.** Every id a caller needs — a `request_id`, a `job_id` — is
  created deterministically in workflow code, so a retried activity retries
  against the same key. `sandbox/contract/types.py` keeps the module
  JSON-native, so Temporal's default data converter round-trips it with no
  custom codec, and frozen, so nothing mutates a value once sent.
- **Two failure classes.** Infrastructure failures raise and are retryable; job
  outcomes — a non-zero exit, a timed-out job — are data, returned in an
  `ExecResult`.
- **Liveness is Temporal's job.** A dead VM shows up as a timeout on its own
  queue, not a special check the contract runs itself.
- **No secrets in inputs.** A caller names a secret; the VM resolves the value.
  [Chapter 04](04-vm-agent.md) covers the VM-side half.

## The types

Every dataclass in `sandbox/contract/types.py`, copied field for field:

| Dataclass | Fields |
|---|---|
| `SandboxSpec` | `pool`, `request_id`, `labels`, `hold_seconds`, `contract_major` |
| `SandboxLease` | `lease_id`, `vm_id`, `task_queue`, `pool`, `contract_version`, `expires_at` |
| `ReleaseRequest` | `vm_id`, `lease_id`, `disposition` |
| `ExecSpec` | `job_id`, `argv`, `cwd`, `env`, `secrets`, `timeout_seconds`, `log_uri` |
| `ExecJob` | `job_id`, `vm_id`, `started_at` |
| `WaitRequest` | `job_id` |
| `CancelRequest` | `job_id`, `grace_seconds` |
| `ExecResult` | `job_id`, `status`, `exit_code`, `stdout_tail`, `stderr_tail`, `log_uri`, `duration_seconds` |
| `PutFileRequest` | `src_uri`, `path` |
| `GetFileRequest` | `path`, `dst_uri` |
| `FileStat` | `path`, `size`, `uri` |
| `ArtifactRequest` | `uri`, `sha256` |
| `ArtifactRef` | `uri`, `sha256`, `path` |
| `VmInfo` | `vm_id`, `agent_version`, `contract_majors`, `uptime_seconds`, `disk_free_bytes`, `running_jobs` |

`ExecResult.status` is one of `EXEC_STATUSES`: `"exited"`, `"timed_out"`,
`"cancelled"`, `"lost"`. `ReleaseRequest.disposition` is one of `DISPOSITIONS`:
`"recycle"`, `"destroy"`.

## The operations

The eleven names built by `activity_name` in `sandbox/contract/names.py`, matched
against the `Lease`/`Sandbox` methods that call them:

| Operation | Queue | Returns | What it does |
|---|---|---|---|
| `acquire` | manager | `SandboxLease` | claims one idle VM in a pool, idempotent on `request_id` |
| `release` | manager | nothing | ends a lease, `recycle` or `destroy` |
| `exec_start` | VM | `ExecJob` | starts a job, idempotent on `job_id` |
| `exec_wait` | VM | `ExecResult` | waits for a started job, reattaches if retried |
| `exec_cancel` | VM | nothing | sends the job's process group SIGTERM then SIGKILL |
| `put_file` | VM | `FileStat` | downloads an object store URI onto the VM |
| `get_file` | VM | `FileStat` | uploads a VM path and returns its URI |
| `ensure_artifact` | VM | `ArtifactRef` | fetches and verifies an artifact by sha256, caching it |
| `describe` | VM | `VmInfo` | reports the VM's own identity and load |
| `snapshot` | VM | `SnapshotRef` | tars a workspace directory to the object store |
| `restore` | VM | `SnapshotRef` | unpacks one on any VM |

`acquire` and `release` are the only two that run on `MANAGER_TASK_QUEUE`; the
other nine are `VM_OPERATIONS`, and only run once a lease names a
`vm_task_queue(vm_id)` for the client to call them on.

## Semantics that matter

**Acquire never blocks.** `Sandbox.acquire` in `sandbox/client/sandbox.py` calls
`sandbox.v1.acquire` with a `RetryPolicy` that treats `NoCapacity` and
`Incompatible` as non-retryable, so a full pool raises immediately rather than
hanging the activity. The client then loops itself: on `NoCapacity` it sleeps
`self.t.acquire_retry_delay` under
`workflow.sleep(..., summary="WaitFor:SandboxCapacity")` and retries with the
same `request_id`, until `acquire_wait` from `Timeouts` runs out, at which
point it raises `SandboxUnavailable`. Reusing the id means a lease claimed just
before a crash is found again by `find_lease_by_request` on the next attempt,
not leaked.

**Exec survives restarts** (spec §5.4). `exec_start` records the job on the VM's
own disk before returning, and returns the existing job if `job_id` is already
known; `exec_wait` reattaches to that record instead of starting a new wait. A
retried activity, after either side restarts, resumes watching the same
process. [Chapter 04](04-vm-agent.md) covers the on-disk job record this
depends on.

**Cancel kills the group** (spec §5.4). Jobs run in their own session via
`setsid`, so cancelling `exec_wait` or calling `exec_cancel` reaches every
process the job started: SIGTERM to the group, a grace period, then SIGKILL.

**Liveness through timeouts.** `sandbox/client/timeouts.py`'s `Timeouts` holds
every duration the client hands to Temporal. `Timeouts.local()` sets
`schedule_to_start=30s, heartbeat=30s`; `Timeouts.prod()` sets
`schedule_to_start=5min, heartbeat=2min, acquire_retry_delay=30s`;
`Timeouts.test()` sets
`schedule_to_start=3s, heartbeat=5s, wait_slack=10s, acquire_retry_delay=500ms, acquire_wait=5s`.
Schedule-to-start firing means the VM's queue went unanswered; heartbeat firing
on `exec_wait` means the job stopped reporting progress.

**Release retries without limit.** `Sandbox.release` runs from `lease()`'s
`finally`, with a `RetryPolicy` that sets no `maximum_attempts`. The module
docstring gives the reason: the manager is idempotent on the lease id, so a
manager outage "should delay the release, not replace the workflow's real
outcome with an error raised while cleaning up." See
[chapter 03](03-lease-lifecycle.md) for the manager side of that idempotency.

## Errors from the caller's seat

`sandbox/contract/errors.py` defines six `SandboxError` subclasses, each an
`ApplicationError` with a stable `type` string so it survives serialization:
`NoCapacity`, `LeaseLost`, `Incompatible`, `SandboxUnavailable`,
`HostDraining`, `ExecFailed`. `translate.py` is the one function mapping a
Temporal `ActivityError` to one of these, or `None` to re-raise the original: a
timeout on a VM call typed schedule-to-start, heartbeat, start-to-close, or
schedule-to-close becomes `LeaseLost`; an `ApplicationError` typed `LeaseLost`
or `HostDraining` also becomes `LeaseLost`; `NoCapacity` and `Incompatible`
pass through as themselves.

`sandbox/client/sandbox.py` defines
`VM_NON_RETRYABLE = (LEASE_LOST, INCOMPATIBLE, HOST_DRAINING)` and passes it as
`non_retryable_error_types` on every VM-queue call. The comment above it
explains `HostDraining`'s place in that tuple: retrying on the VM's own queue
"is retrying against a machine that will not answer differently"; the only
worker that could pick up the retry is the one shutting down, so translating it
to `LeaseLost` immediately lets the caller acquire another VM. Once
`Lease._call` maps a failure to `LeaseLost`, it sets `self.lost = True`, so
every later call on that lease raises `LeaseLost` locally instead of hammering
a dead queue. `ExecFailed`, raised by `Lease.exec(..., check=True)` on a
non-zero exit or a non-`"exited"` status, carries the result's stderr tail;
`SandboxUnavailable` comes only from the acquire loop's own timeout.

## Using it

`SmokeWorkflow` in `sandbox/orchestrator/workflows.py` is the smallest use of
the client, and its whole body fits the shape every other workflow follows:

```python
sandbox = Sandbox(Timeouts.for_profile(params.profile), workspace_root=...)
spec = SandboxSpec(pool=params.pool, request_id=str(workflow.uuid4()))
async with sandbox.lease(spec) as vm:
    info = await vm.describe()
    uname = await vm.exec(ExecSpec(job_id=vm.new_job_id(), argv=["uname", "-a"], ...))
    whoami = await vm.exec(ExecSpec(job_id=vm.new_job_id(), argv=["id"], ...))
    return SmokeResult(vm_id=info.vm_id, lease_id=vm.lease.lease_id, ...)
```

`sandbox.lease(spec)` is `Sandbox.lease()`, an `@asynccontextmanager` calling
`acquire`, yielding the `Lease`, and calling `release` in `finally` no matter
how the block exits. Everything inside the `async with` runs against
`vm.lease.task_queue`, the VM's own queue, never touching the manager again
until release.

## Read the code

- `sandbox/contract/version.py` — `CONTRACT_MAJOR`, `CONTRACT_VERSION`.
- `sandbox/contract/names.py` — `activity_name`, `vm_task_queue`, the eleven
  operation constants, `VM_OPERATIONS`.
- `sandbox/contract/types.py` — every dataclass this chapter's table lists.
- `sandbox/contract/errors.py` — the six error types, `SandboxError`.
- `sandbox/client/sandbox.py` — `Sandbox.acquire`, `.release`, `.lease()`,
  `Lease`'s methods, `VM_NON_RETRYABLE`.
- `sandbox/client/timeouts.py` — `Timeouts.local`, `.prod`, `.test`.
- `sandbox/client/translate.py` — `translate`, the one mapping function.
- `sandbox/orchestrator/workflows.py` — `SmokeWorkflow`, the worked example.

## Where this maps in production

`version.py`, `names.py`, `types.py`, `errors.py`, and `translate.py` each say
"Production: identical"; `sandbox.py` says "identical, on the default root."
`types.py` adds the one rule that changes anything: a field added with a
default is a minor version and needs no rename; removing or renaming one is a
major version and gets new activity names, so a rollout can serve both while
the fleet catches up. `timeouts.py` names its production form directly:
`Timeouts.prod()`, the same dataclass with larger numbers.

## Try it

Run `make smoke`. It starts `SmokeWorkflow` on `orchestrator-queue` and prints a
Temporal UI link before the result. Open it and find, in order:
`sandbox.v1.acquire` on `sandbox-manager-queue`; then `sandbox.v1.describe` and
two pairs of `sandbox.v1.exec_start` / `sandbox.v1.exec_wait`, all four on
`sandbox-vm-<vm_id>`; and finally `sandbox.v1.release` back on
`sandbox-manager-queue`. The printed `uname` and `whoami` lines are the
`stdout_tail` fields of the two `ExecResult`s the workflow returned.

Next: [03 · Leases, the registry, and the reconciler](03-lease-lifecycle.md)
