# 03 · Leases, the registry, and the reconciler

After this chapter you can draw a VM's states from boot to termination, name the
condition expression that makes claiming a VM exactly-once, and walk the five
steps a reconciler pass runs to hold a pool at its floor.

## VM states

`sandbox/registry/schema.py`'s `STATES` tuple is the vocabulary: `booting`,
`idle`, `leased`, `recycling`, `draining`, `terminating`, `dead`, `terminated`.
`dead` is reserved — no writer sets it; `write_off` always sets `terminated`.
The shape below matches spec §6.1, redrawn for the transitions the code
performs:

```
launch ──▶ booting ──▶ idle ──▶ leased ──▶ recycling ──▶ idle
              │          │         │           (VM agent wipes it, ch. 04)
              │          │         ├──▶ terminating ──▶ terminated  (release: destroy)
              │          │         └──▶ draining ──▶ terminated     (VM agent: SIGTERM, ch. 04)
              │          └──▶ terminating ──▶ terminated            (reconciler: scale-in)
              └──▶ terminated                                       (boot deadline)
any live state ──▶ terminated   (reconciler write-off: instance missing or stopped,
                                 stale heartbeat, boot deadline, stuck transition)
```

A `terminated` row carries a `ttl` — `set_state` and `write_off` both set it
with `epoch_in(3600)` — so the reconciler's sweep, inside its health step, not
just DynamoDB's TTL delivery, clears it after `sweep_after_seconds`.

## The registry

Four DynamoDB tables, created by `create_tables` in `schema.py`: `sandbox_vms`
(partition `vm_id`, with `pool_state_index`, `lease_request_index`,
`lease_index` as global secondary indexes), `sandbox_jobs` (partition `vm_id`,
sort `job_id`), `sandbox_events` (partition `day`, sort `ts_ulid`),
`sandbox_requests` (partition `request_id`). `client.py`'s docstring states the
point plainly: "conditional writes are the whole point."

`claim_idle` queries `pool_state_index` for `(pool, idle)`, sorts the candidates
oldest-first by `created_at`, then for each issues `update_item` with
`ConditionExpression="#s = :idle AND attribute_not_exists(lease_id)"`, so the
oldest VM is leased first; two managers racing one row cannot both succeed.
`release` conditions on `"lease_id = :lease"`: if the row no longer carries
that id, it returns `False` having done nothing, because the lease already
ended. `write_off` conditions on
`"attribute_exists(vm_id) AND #s <> :terminated"`, so writing off a VM twice is
a no-op returning `False` — its docstring explains why: a second write would
reset `last_transition_at` and the TTL, "so a row could be kept out of the
sweep forever." `touch_lease` conditions on `"lease_id = :lease"` too, so an
ended lease cannot be revived by a late heartbeat. `register_vm`'s docstring
describes the same care: a re-registering agent must never reset a leased row
back to unleased `booting`, so its first write only succeeds for a new `vm_id`
or a row already `terminated` or `dead`; an existing row still holding
`lease_id` falls back to updating identity fields only, leaving `state`
untouched.

## Acquire and release

`ManagerActivities.acquire`, in `sandbox/manager/activities.py`, first calls
`find_lease_by_request(spec.request_id)`: a lease already claimed under this id
is returned again, so a retrying workflow never leaks a VM. Otherwise it checks
`spec.contract_major` against `SUPPORTED_MAJORS`, raising `Incompatible` on a
mismatch, then calls `claim_idle`. On success it fulfills the request and emits
`acquire`; on no match it calls `registry.record_pending(...)` and raises
`NoCapacity`. That pending record feeds the reconciler: its capacity step adds
`len(inv.pending)` into the deficit, so a workflow stuck waiting for capacity
is what grows the pool.

`.release` is idempotent on `lease_id`. `recycle` calls `registry.release`,
flipping the row to `recycling`; the VM agent finishes the transition to `idle`
on its next heartbeat ([chapter 04](04-vm-agent.md)). `destroy` terminates
through the provider first — the comment above it explains why: "if the
provider call fails with the row already unleased, a retry finds nothing to do
and the VM is stranded in `terminating`" — then releases and sets `terminated`.

## Lease expiry and renewal

Every VM-queue activity calls `self._touch()`, which runs
`registry.touch_lease(vm_id)` — `sandbox/vm_agent/activities.py` wires this
into all nine VM operations. `touch_lease` extends `lease_expires_at` by the
`hold_seconds` recorded at claim time and bumps `last_heartbeat_at`, so a lease
under active use never expires from age alone. Spec §6.5: the real orphan
signal is owner-workflow liveness, not expiry — the backstop for a caller that
stops cooperating, which the leases step checks next.

## The reconciler

`sandbox/manager/reconciler/core.py`'s module docstring names the shape directly:
"five steps over one inventory snapshot, health, leases, capacity, requests,
sample, each returning the actions it took." The `Reconciler` class it
describes is provider- and Temporal-free — `ReconcileWorkflow` wraps each step
in its own activity, so a failing step retries alone and the UI shows what a
pass did. Its docstring gives the call order as "inventory, health, inventory,
leases, capacity, sample" — a fresh inventory before `leases` because `health`
may have written VMs off since the first snapshot, and the request sweep runs
inside `capacity` on that snapshot rather than owning a step of its own.

**Health** terminates provider instances with no registry row past the boot
deadline, or reported in
`DEAD_STATES = frozenset({"stopped", "stopping", "exited", "dead"})` — a strict
list on purpose: an unrecognised state is treated as alive, so a vocabulary
change cannot be read as a fleet-wide death sentence, and a truly gone VM is
still reaped by the boot deadline or a stale heartbeat. If the provider reports
no instances while live rows exist, `health` sets `inventory_suspect` and skips
missing-instance write-offs for that pass — every other check still runs —
because a vanished fleet is far less likely than one bad provider call.

**Leases** releases any `leased` row whose owner is closed or cannot be found,
or whose `lease_expires_at` has passed while it is not `RUNNING`.

**Capacity** computes `deficit = policy.min_idle + len(inv.pending) - available`
and launches up to `min(deficit, policy.max - total)` VMs; a failed launch
emits `launch_failed` and stops the step for that pass. With no deficit, no
pending requests, and idle above the floor past the cooldown, it retires the
single oldest unprotected idle VM. It then runs `requests`, abandoning pending
requests older than `abandon_after_seconds`.

**Sample** emits a `fleet_sample` event with ten counts: `total`, `idle`,
`leased`, `booting`, `recycling`, `draining`, `dead`, `pending`, `min_idle`,
`max`.

`Tunables.local()` returns
`(boot_deadline=120s, stale_after=30s, stuck_after=300s, scale_in_cooldown=120s, abandon_after=600s, sweep_after=600s)`;
`Tunables.prod()` returns `(600, 180, 300, 600, 600, 3600)` — the same six
knobs, wider in production.

`reconciler/schedule.py`'s `ensure_schedule` creates or updates a Temporal
Schedule, `sandbox-reconcile-<pool>`, with `overlap=SKIP`, so a pass never
starts beside one still running. The manager worker calls it at startup;
`make reconcile` triggers that schedule rather than starting a second workflow,
so a manual pass keeps the same overlap policy. After changing
`reconciler/workflow.py` or `activities.py`, restart the manager worker:
Temporal re-imports workflow code on every task, but a running worker keeps
the activity code it started with.

## Pool policy

`PoolPolicy`, in `sandbox/manager/policy.py`, holds `pool`, `min_idle`, `max`,
`image`, `cpus`, `memory`. It lives as one item per pool in `sandbox_vms` at
`vm_id = "pool#<name>"`. `list_vms` excludes every such row from ordinary VM
queries (a Python filter on `vm_id`); `list_pools` is the mirror image,
selecting only pool rows with a DynamoDB `begins_with` filter. `make policy`
runs `policy_cli`: with no flags it prints the current policy, or merges
`--min-idle` and `--max` (`MIN_IDLE=`, `MAX=`) into it and writes it back;
`--image` only when calling `policy_cli` directly. The reconciler picks up any
change on its next pass.

## Read the code

- `sandbox/registry/schema.py` — `STATES`, the four tables and indexes.
- `sandbox/registry/client.py` — `claim_idle`, `release`, `write_off`,
  `touch_lease`, `register_vm`, and their condition expressions.
- `sandbox/manager/activities.py` — `ManagerActivities.acquire`, `.release`.
- `sandbox/manager/policy.py` — `PoolPolicy` and its fields.
- `sandbox/manager/reconciler/core.py` — `Reconciler`, `Tunables`, `DEAD_STATES`,
  `inventory_suspect`.
- `sandbox/manager/reconciler/workflow.py` — `ReconcileWorkflow`, the step order.
- `sandbox/manager/reconciler/activities.py` — one activity per step.
- `sandbox/manager/reconciler/types.py` — `Inventory`, `Action`,
  `ReconcileReport`.
- `sandbox/manager/reconciler/schedule.py` — `ensure_schedule`, the schedule and overlap
  policy.

## Where this maps in production

`schema.py` says tables are "created by Terraform" in production, with
`create_tables` reserved for local runs and tests — the same DynamoDB API
either way, only the endpoint changes. `client.py` is "identical," with the VM
agent's write access narrowed to its own `vm_id` by IAM. `reconciler/core.py` is
"identical; the tunables grow and the provider is EC2." `workflow.py` runs "on
a one-minute schedule" instead of every fifteen seconds.

## Try it

Run `make show` for the current pools, the latest fleet sample, every VM row,
and recent events — the terminal form of [chapter 07](07-dashboard.md)'s
dashboard. Run `make hold`, note the workflow id printed, then
`make terminate WF=<that id>` — `terminate` skips the `finally`, so the lease
becomes an orphan with no `release` ever running. Run `make reconcile` to force
an immediate pass: the leases step finds the owner terminated and releases the
row to `recycling`, and the next heartbeat wipes it to `idle`. Run
`make policy MIN_IDLE=1` to lower the floor and watch the next pass's
`scale_in` retire the oldest idle VM once its 120-second cooldown has passed;
`make policy MAX=3` only caps how many launches a deficit may cause.

Next: [04 · Inside a VM: the agent that runs jobs](04-vm-agent.md)
