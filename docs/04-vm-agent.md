# 04 · Inside a VM: the agent that runs jobs

After this chapter you can trace a VM from boot to answering
`sandbox.v1.describe`, name which of its two users owns which directory and
why a job can only cross between them through one sudo rule, and explain
what a job directory on disk must contain for `exec_start` and `exec_wait`
to survive a restart.

## Boot

`sandbox/vm_agent/worker.py`, the entry point `images/vm/entrypoint.sh`
execs, builds an `AgentConfig` from `AgentConfig.from_env()`, connects a
Temporal `Client`, builds an `AgentRuntime`, installs `request_drain` as
the `SIGTERM`/`SIGINT` handler, and awaits `run_until_stopped()`.
`AgentConfig.from_env` (`sandbox/vm_agent/config.py`) reads what the
provider passes at launch: `VM_ID` (required — missing it is a `KeyError`),
`POOL`, `PROVIDER_REF`, the Temporal address, the state directories,
`SANDBOX_RUN_AS_USER`, `SANDBOX_HEARTBEAT_SECONDS` (default `5`), and a
`PREFETCH_ARTIFACTS` list of `uri@sha256` pairs.

`AgentRuntime.start` (`sandbox/vm_agent/runtime.py`) then creates the state
directories; calls `registry.register_vm`, writing the `booting` row with
`SUPPORTED_MAJORS` from `sandbox/contract/version.py`; fetches each
prefetch pair into the artifact cache; starts a Temporal `Worker` on
`vm_task_queue(cfg.vm_id)` carrying all seven activities; flips the row to
`idle` (conditional on `booting`); emits a `boot` event; and starts the
heartbeat loop.

## Two users and one sudo rule

`images/vm/Dockerfile` creates two users: `agent`, owner of the workspace
`/private/tmp/sandbox`, and `sandbox-agent`, owner of `/var/lib/sandbox` and
`/etc/sandbox/secrets`, which runs the Temporal worker. One sudoers line is
the whole boundary: `sandbox-agent ALL=(agent) NOPASSWD: ALL`.

`sandbox/vm_agent/jobs.py`'s docstring names why a job crosses that
boundary through `sudo --preserve-env`: "the job's environment crosses in
the process environment rather than on the command line, where
`/proc/<pid>/cmdline` would expose every resolved secret to any local
reader." `JobStore._wrap` reaches for `sudo` only when `run_as_user` is
set. It cuts both ways — "the worker cannot signal what it launched
through sudo, so every kill goes back through sudo as well" —
`_signal_group` signals the group as `sandbox-agent` (reaching only the
wrapper shell) and again through `sudo` as the job user, "where the job
user is allowed to signal its own."

## Job records

`JobStore` keeps one directory per job under `cfg.jobs_dir`: `spec.json`,
written before spawning; an atomically-renamed `meta.json` holding
`job_id`, `pid`, `pgid`, `started_at`; `stdout.log`/`stderr.log`; and an
`exit` file the wrapper shell itself writes when the command ends —
`_WRAPPER` is `'"$@"; rc=$?; printf "%s" "$rc" > "$0/exit.tmp"; mv
"$0/exit.tmp" "$0/exit"; exit "$rc"'`, `$0` the job directory. A `reason`
file records why something outside the job ended it; a `lost` marker is
dated by whichever caller first notices the process gone with no exit file.

`exec_start` is idempotent: `JobStore.start` tries an exclusive `mkdir`
first, and on `FileExistsError` waits briefly for `meta.json`, returning
the existing `ExecJob` on a matching `job_id` (a different one collides,
raising `Incompatible`), or removes a leftover directory and retries.
`exec_wait` reads the same record, so a retried wait reattaches instead of
starting a new job; an unrecognized `job_id` raises `Incompatible`, since
retrying cannot make a job appear on a machine that never started it.

## The seven activities

`VmActivities.all()` lists the seven names chapter 02 called
`VM_OPERATIONS`: `exec_start`, `exec_wait`, `exec_cancel`, `put_file`,
`get_file`, `ensure_artifact`, `describe`. Every one calls `self._touch()`
(`registry.touch_lease`) — a no-op on an unleased VM, otherwise what keeps
a lease alive under a running job.

`exec_start` validates `cwd` under the workspace root, records the
*validated* path rather than the caller's string, and builds the
environment with `_job_env`. `exec_wait` polls every second, checking
`self.drain.draining` before the job's own liveness (a drain's kill must
never read as an ordinary finish), and times the job out past
`spec.timeout_seconds`. `exec_cancel` sends `SIGTERM` then `SIGKILL`
through `JobStore.cancel`. `put_file`/`get_file` (`sandbox/vm_agent/files.py`)
validate the workspace path and stream through `ObjectStore`;
`ensure_artifact` (`sandbox/vm_agent/artifacts.py`) caches a `.tar.gz` by
verified `sha256`; `describe` reports version, `SUPPORTED_MAJORS`, uptime,
free disk, and running-job count.

`_job_env` is where `sandbox/vm_agent/validation.py`'s two checks meet.
`validate_env` rejects a key that is not a legal variable name, that would
steer the wrapper (`BASH_ENV`, `ENV`, `LD_`/`DYLD_`), or that looks like a
credential (`AWS_`-prefixed, or `_TOKEN`/`_SECRET`/`_KEY`-suffixed) — those
must be named as secrets, resolved by `resolve_secrets` from a file under
`cfg.secrets_dir`, uppercased as the key. `_job_env` also pops and
reinjects `_VM_IDENTITY_KEYS` (`S3_ENDPOINT`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`) from the agent's own
environment, never the caller's: injecting conditionally alone "is exactly
how a request would redirect the VM at an endpoint it chose."

## Heartbeat, recycle, wipe

`HeartbeatLoop` ticks every `cfg.heartbeat_seconds` (5 locally, 30 in
production), calling `registry.heartbeat(vm_id)` to bump
`last_heartbeat_at` and read the row's state. A missing row, or one already
in `WRITTEN_OFF` (`"terminated"`, `"dead"`), means the reconciler gave up:
the loop stops and the process exits rather than heartbeat a row nobody
will claim again. A `recycling` state means the lease ended: the loop runs
`wipe()`, sets the row to `idle` (conditional on `recycling`), and emits a
`wipe` event — the transition chapter 03 named "the VM agent wipes it." A
registry error is logged and retried, not fatal.

`wipe`, in `sandbox/vm_agent/wipe.py`, cancels every running job, then a
`sudo`-routed `pkill` and `find` clear what `agent` left behind (plain
argv, so a directory name cannot become a shell command); `jobs.prune`
deletes job directories over an hour old, leaving the artifact cache alone.

## Drain

`SIGTERM` reaches `runtime.request_drain`, which starts `AgentRuntime.drain`
as a background task, since a signal handler cannot `await` one directly.
`drain` marks `drain_state.draining`, writes the row to `draining`, cancels
every running job with a 10-second grace, then its `finally` clause stops
the runtime to `terminated` regardless of whether the registry write or a
kill succeeded. An `exec_wait` in flight notices the flag, flushes its logs, and
raises `HostDraining()`, whose `__init__` (`sandbox/contract/errors.py`)
fills in `non_retryable=False` and a one-second `next_retry_delay`. Chapter
02 covered why the client still treats it as fatal on a VM's own queue: a
retry there can only reach the worker shutting down, so `translate.py`
turns it into `LeaseLost` and the caller acquires another VM.

## The image

`images/vm/Dockerfile` calls itself a stand-in "for the Packer AMI." From
`ubuntu:24.04` it installs Python, `git`, `sudo`, and process tools; creates
the two users and state directories; and installs `sandbox` into a venv
from a lockfile `scripts/build-image.sh` exports, `pip install --no-deps`
on top so the image never resolves a version the tests have not seen. It
seeds the demo's clone repositories and hands them to `agent` because "git
refuses to read a repository owned by another user (its safe.directory
check), so permissions alone are not enough — the seed is handed to agent,
the user that actually clones it." `images/vm/entrypoint.sh` runs as root
only long enough to `chown` the state directories, then `exec`s `setpriv
--reuid=sandbox-agent --regid=sandbox-agent` into `sandbox.vm_agent.worker`;
`container run --init` supplies the init process that forwards `SIGTERM`
into the drain above.

## Read the code

- `sandbox/vm_agent/config.py` — `AgentConfig.from_env`, the boot variables.
- `sandbox/vm_agent/runtime.py` — `AgentRuntime.start`, `.drain`, `.stop`.
- `sandbox/vm_agent/activities.py` — `VmActivities`, `_job_env`,
  `_VM_IDENTITY_KEYS`.
- `sandbox/vm_agent/jobs.py` — `JobStore`, `_WRAPPER`, `_wrap`,
  `_signal_group`, `_survivors`, `cancel`.
- `sandbox/vm_agent/validation.py` — `validate_env`, `resolve_secrets`.
- `sandbox/vm_agent/files.py` — `put_file`, `get_file`.
- `sandbox/vm_agent/artifacts.py` — `ArtifactCache.ensure`.
- `sandbox/vm_agent/heartbeat.py` — `HeartbeatLoop`, `WRITTEN_OFF`.
- `sandbox/vm_agent/drain.py` — `DrainState`.
- `sandbox/vm_agent/wipe.py` — `wipe`.
- `sandbox/vm_agent/worker.py` — `main`, the entry point.
- `images/vm/Dockerfile` — the two users, the sudoers rule.
- `images/vm/entrypoint.sh` — the chown-then-`setpriv` sequence.

## Where this maps in production

`runtime.py`, `activities.py`, `files.py`, `artifacts.py`, `jobs.py`, and
`validation.py` each say "Production: identical" in their own docstring.
`config.py` names the one thing that changes: "the same variables come from
user_data on EC2. `run_as_user` is `agent` there and in the image; tests
leave it unset and run jobs as themselves." `heartbeat.py` runs at 30
seconds instead of 5, and `drain.py`/`worker.py` trigger from the EC2 IMDS
lifecycle watcher instead of a signal. Nothing about the two users, the
sudo rule, or the job records changes on a real EC2 instance.

## Try it

Run `make vms` to list the running Apple containers by name and state.
Start `make hold` in one terminal, note a leased VM's id from `make show`,
then run `make chaos-stop VM=<that id>` in another. Watch `make show`: the
row moves to `draining` as this chapter's drain runs, then to `terminated`
once the agent's own `finally` clause stops the worker — before the
reconciler's next pass gets a chance to notice.

Next: [05 · The provider: where VMs come from](05-provider.md)
