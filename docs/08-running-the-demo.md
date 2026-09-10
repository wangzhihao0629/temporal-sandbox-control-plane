# 08 · Running the demo

After this chapter you can bring the whole stack up from nothing, run each of
the twelve demo steps and say what command drives it and what code path it
exercises, and put it back down cleanly — or explain from the scripts alone
what a step you have not run would do.

## Before you start

Spec §12.1 names the tools: Apple silicon and macOS running Apple's
`container` CLI (Homebrew formula `container`), the `temporal` CLI, `uv`, and
Python 3.12 — `container` is what requires the silicon and the OS version.
`make bootstrap` checks the first two directly: it fails with an install hint
if `command -v temporal` or `command -v container` comes back empty, then
runs `uv sync` for everything else.

## Bring the stack up

`make up` runs `scripts/up.sh`. In order: `container system start` (best
effort — an already-running daemon is fine); a Temporal dev server backed by
`.local/temporal.db`, started only if port `7233` is not already answering;
`moto_server` standing in for DynamoDB and S3, checked against `/moto-api/`
before assuming a listener on that port is actually moto; then a throwaway
`alpine` container fetches from moto through the bridge gateway
(`192.168.64.1` by default, `SANDBOX_VM_GATEWAY` to override), proving a VM
can reach the host before anything depends on it. The script's own comment
gives the reason moto runs on the host at all: a managed laptop confines host
processes to loopback, so the `container` bridge network the VMs share is
unreachable from here, even though a VM can reach a host listener over that
same bridge.

Only then does it write `.env`: `TEMPORAL_ADDRESS`/`TEMPORAL_NAMESPACE`/
`TEMPORAL_UI` and `VM_TEMPORAL_ADDRESS` (loopback for host processes, the
gateway for VMs); `DYNAMODB_ENDPOINT`/`S3_ENDPOINT` and their `VM_` pairs the
same way; local AWS credentials moto ignores but every SDK call still needs;
`SANDBOX_VM_IMAGE`, `SANDBOX_PROFILE`, `STATUS_PORT`, `STATUS_DEMO_MODE`.
Last, `sandbox/bootstrap.py` creates the four tables and four buckets and
seeds pool `demo`'s policy at `min_idle=2`, `max=5`. `make image`
(`scripts/build-image.sh`) exports the locked dependency set and hands the
repo root to `container build`; pulling and layering `ubuntu:24.04` is the
slow step, done once until the Dockerfile or the lockfile moves. `make
artifact` (`sandbox/runner/package.py`) is fast by comparison: a handful of
small `.py` files packed into a deterministic `.tar.gz` in memory and
uploaded, then `RUNNER_URI`/`RUNNER_SHA256` written into `.env` —
`sandbox/envfile.py`'s `write` leaves every other key where `up.sh` put it.
`make workers` starts the three `Procfile` processes under `honcho`; the
reconciler's first passes top the pool up from nothing.

## Twelve steps

Spec §13 names twelve steps. Each one below names its command, what to watch
in the dashboard and the Temporal UI, and the file behind it.

### 1. Cold start

`make up`, then `make workers`. Dashboard: the counters climb `booting` then
`idle` as `sandbox/manager/reconciler.py`'s capacity step launches two VMs and
each one's own `AgentRuntime.start` (`sandbox/vm_agent/runtime.py`) flips its
row once its Temporal worker is up. Temporal UI: nothing to watch yet besides
the `sandbox-reconcile-demo` schedule firing every 15 seconds
(`sandbox/manager/worker.py`'s `SANDBOX_RECONCILE_INTERVAL_SECONDS` default).

### 2. Smoke

`make smoke`. Dashboard: a row goes `leased` and back to `idle` in seconds.
Temporal UI: the acquire/describe/exec/release sequence chapter 02's "Try it"
already walked through, activity by activity. Code path:
`sandbox/orchestrator/run_smoke.py`, `SmokeWorkflow` in
`sandbox/orchestrator/workflows.py`.

### 3. A session with a fix loop

`make demo` (packages the artifact on a first run) or `make session
SCENARIO=multiply-with-bug`. Turn one adds `multiply` with `+`; the test step
reports one failure; turn two quotes it and fixes the operator. Dashboard: two
turns in the session's card, `fake_cost_usd` climbing, `[think]`/`[tool]`/
`[feedback]` lines in the log tail. Temporal UI: `turn`, `lint`, and `test`
exec pairs repeated twice on the same VM queue. Code path:
`CodingSessionDemoWorkflow`, `sandbox/runner/scenarios.py`'s
`multiply-with-bug`.

### 4. A clean session

`make session SCENARIO=divide-clean`. One turn, a zero-guarded `divide`, green
tests immediately. Verified live below: one turn, tests pass, zero lint
findings, one lease attempt. Compare its shorter Temporal history side by side
with step 3's two-turn one — same workflow, same activities, half the
history. Code path: same workflow, `scenarios.py`'s `divide-clean`.

### 5. Orchestrator restart

While a turn is running, `pkill -f sandbox.orchestrator.worker`, then bring it
back with `uv run python -m sandbox.orchestrator.worker` — the same command
the `Procfile`'s `orchestrator` line runs. Dashboard: nothing changes on the
VM side. Temporal UI: the in-flight `exec_wait` activity simply waits longer
for a worker to poll `orchestrator-queue` again; no retry, no rerun. Code
path: chapter 02's "exec survives restarts" — `JobStore.start` and
`exec_wait`'s reattachment in `sandbox/vm_agent/jobs.py` and `activities.py`.

### 6. VM death

`make chaos-kill VM=<leased vm id>` mid-turn. Dashboard: `write_off_stopped`,
then a replacement `launch` event, the same pair chapter 05's own drill
produces. Temporal UI: the interrupted `exec_wait` fails, followed by a
fresh `acquire`. What is new here over chapter 06's own version of this
drill: the workflow's printed result names two lease attempts and two VM
ids for one session. Code path: `sandbox/manager/chaos.py`'s `apply`, the
health step in `reconciler.py`, the bundle resume in `session.py`.

### 7. Orphan

`make hold`, note the printed workflow id, then `make terminate WF=<that
id>` — the exact drill chapter 03's "Try it" already ran, restated here in
its place in the twelve-step sequence rather than in isolation. Dashboard:
the row stays `leased` with its owner shown `Terminated` until the next
reconcile pass releases it to `recycling`. Temporal UI: the held workflow's
history ends in `Terminated` with no `release` activity ever recorded. Code
path: `sandbox/orchestrator/terminate.py`, the leases step in
`reconciler.py`.

### 8. Manager outage

`pkill -f sandbox.manager.worker` while a turn is running: it keeps going,
since execution never touches the manager once leased. Start a second `make
session`; watch it block on `WaitFor:SandboxCapacity` instead of failing.
Restart with `uv run python -m sandbox.manager.worker` and watch it proceed.
Dashboard: the fleet counters stop refreshing until the reconciler resumes
emitting samples. Code path: `Sandbox.acquire`'s retry loop in
`sandbox/client/sandbox.py`, `acquire_retry_delay` in
`sandbox/client/timeouts.py`.

### 9. Capacity

Three `make session` runs against a two-VM floor. The third's `acquire`
raises `NoCapacity`, records a pending request, and sleeps under
`WaitFor:SandboxCapacity`; the reconciler's capacity step folds that pending
count into its deficit and launches a third VM, since the pool's `max` still
has room, and the third session proceeds. Dashboard: a `launch` event and a
third row climbing to `leased`. Code path: `record_pending` in
`sandbox/manager/activities.py`, the `deficit = policy.min_idle +
len(inv.pending) - available` line in `reconciler.py`.

### 10. Drain

`make chaos-stop VM=<leased vm id>` mid-turn — chapter 04's drain mechanism
and chapter 05's chaos mapping, run against a lease in use rather than an
idle VM. Dashboard: `leased` → `draining` → `terminated` from inside the VM,
no reconciler pass needed; a fresh `acquire` re-dispatches within seconds.
Temporal UI: the in-flight `exec_wait` fails with `HostDraining`, translated
to `LeaseLost`. Code path: `sandbox/vm_agent/drain.py`,
`sandbox/client/translate.py`.

### 11. Giving up

`make session SCENARIO=never-fixes`. Turn one repeats `multiply-with-bug`'s
broken edit; every turn after leaves a comment instead of fixing it, and
`Scenario.turn_for` keeps repeating that last scripted turn until `max_turns`
(three by default). Dashboard: `export` still runs and the summary still
publishes, with `tests_passed: false` — exhausting the loop is a result, not
a failure. Code path: `never-fixes` in `scenarios.py`, the `done = ... or
turn_no >= p.max_turns` check in `CodingSessionDemoWorkflow`.

### 12. Scale in

Once the sessions above finish and the idle VMs they leave behind sit past
the 120-second cooldown, the reconciler's capacity step retires the single
idle VM whose last transition has actually cleared that cooldown — never one
still holding a `protected` flag or one that only just went idle. Code path:
the scale-in branch of `reconciler.py`'s `capacity`, candidates sorted by
`created_at`.

## Tear it down

`make down` runs `scripts/down.sh`: it deletes every `sbx-*` container, two
leftovers from before moto replaced DynamoDB Local and MinIO, stops
`moto_server` and the Temporal dev server by their recorded pids, and
deletes `.env`. It never touches `.local/temporal.db` or the logs beside it,
so the dev server's workflow history survives a down-and-up cycle — old
workflow ids are still there to find in the UI afterward.

## When something is off

Editing `sandbox/manager/reconcile.py` or `reconcile_activities.py` without
restarting is the stale-worker symptom chapter 03 named: the workflow reloads
from disk on every task, but a running worker keeps the activity code it
started with. Restart with `make workers`. When the dashboard itself looks
broken, fall back to `make show` — `sandbox/registry/show.py` calls itself
"the dashboard before there is a dashboard." Three integration tests are
wall-clock-sensitive on purpose:
`test_a_crashed_vm_is_written_off_and_replaced`,
`test_pending_demand_launches_and_surplus_scales_in`, and
`test_stale_pending_requests_are_abandoned` in
`tests/integration/test_reconcile.py`, each sleeping past a `Tunables.test()`
threshold plus half a second before asserting a pass acted on it; a slow or
loaded machine can push one close to that margin without anything being
broken.

## Read the code

- `Makefile` — every target this chapter names.
- `scripts/up.sh` — the boot sequence and the `.env` it writes.
- `scripts/down.sh` — what a teardown deletes and what it leaves alone.
- `scripts/demo.sh` — the cold-start wait loop behind `make demo`.
- `scripts/build-image.sh` — the locked export `make image` builds from.
- `Procfile` — the three processes `make workers` starts.
- `sandbox/bootstrap.py` — the default pool policy `make up` seeds.
- `sandbox/orchestrator/run_smoke.py`, `run_session.py`, `run_hold.py`,
  `terminate.py` — the CLIs behind `make smoke`, `session`, `hold`,
  `terminate`.
- `sandbox/manager/chaos.py`, `policy_cli.py`, `reconcile_once.py`,
  `launch_vm.py` — the CLIs behind `make chaos-kill`, `chaos-stop`,
  `chaos-delete`, `policy`, `reconcile`, `vm`.
- `sandbox/registry/show.py` — `make show`'s output.
- `tests/integration/test_reconcile.py` — the three wall-clock-sensitive
  tests.

## Where this maps in production

Nothing in this chapter's own layer ships anywhere — spec §12's tools, ports,
and make targets are laptop ergonomics around code every earlier chapter
already named a production successor for. `make up`, `image`, and `workers`
become platform worker bootstrap-managed EKS deploys of the same
orchestrator, manager, and VM agent packages; `make session`/`demo` become a
self-service portal action or a real `ProductionCodingWorkflow` start; the CLIs
under `sandbox/manager` become the dashboard's own write endpoints chapter 07
already covers, or an operator action from the Temporal UI directly. Only the
entry point changes.

## Try it

Everything above is what to run; here is what running it showed against this
stack. `make show` reported the pool at its floor — `pool demo: floor 2, max
5, image sandbox-vm:dev` — two `idle` VMs, `fleet_sample` events landing
every 15 seconds. `make policy` printed `pool demo: min_idle=2 max=5
image=sandbox-vm:dev cpus=2 memory=2048M`. `curl -fsS
http://127.0.0.1:8600/api/config` reported `"demo_mode":true`; `/api/fleet`
reported `"source":"sample"` with the same two-idle count. `make session
SCENARIO=divide-clean` then printed `done: 1 turn(s), tests pass, 0 lint
finding(s)`, `vms: sbx-5a1c840c (1 lease attempt(s))`, `cost: $0.0770 (fake)`.
That run also showed steps 9 and 12 without a separate drill: leasing the VM
dropped idle below the floor and the reconciler launched a third; releasing
it afterward pushed idle to three, and the reconciler retired the one idle VM
whose last transition had already cleared the cooldown — a `launch`
followed, minutes later, by a `scale_in`, exactly as `make show`'s event log
recorded them.

Next: [09 · From demo to production](09-from-demo-to-production.md)
