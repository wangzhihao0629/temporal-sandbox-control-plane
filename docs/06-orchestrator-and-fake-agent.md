# 06 · A coding session: the orchestrator and the fake agent

After this chapter you can trace `CodingSessionDemoWorkflow` from clone to
summary, name which of `run_step`'s outcomes means a bug and which means the
step did its job, explain how a session resumes on a new VM without redoing
finished work, and say what changes and what does not when the fake agent is
swapped for `claude -p`.

## The shape of a session

`CodingSessionDemoWorkflow`'s own docstring calls it "a coding session the way
the production agent workflow runs one": clone, then turn, lint, and
test until tests pass or `max_turns` is reached, then export and publish a
summary. Every step is its own exec activity, so a Temporal UI search or a
job log filename names the step without opening an envelope — `steps.py`'s
`job_id` builds each one as `f"{ctx.session_id}-{step}-t{turn}-a{ctx.attempt}"`.

Clone always runs as turn `0`. A turn already greater than zero means a
previous lease persisted work this workflow never saw, so it runs `lint` and
`test` on that turn first rather than assuming another is needed. Otherwise
it loops `turn`, `lint`, `test` until a test envelope says `failed == 0` or
the turn count reaches `max_turns`, then `export` writes a patch `get_file`
pulls off the VM, and `publish_summary` writes `summary.json` — per
`sandbox/orchestrator/activities.py`'s own docstring, "the summary stands in
for opening a pull request: everything a reviewer or a dashboard needs, in
one object with a predictable key."

A turn may run for hours: `turn_timeout_seconds` (3600) and
`step_timeout_seconds` (600) are its only bounds; heartbeats renew the lease.

`SessionUris` builds every path from the session id: `session` is
`s3://sandbox-sessions/<session_id>`, `envelope(job_id)` is
`{session}/steps/{job_id}.json`, `log(job_id)` is its own prefix under
`s3://sandbox-jobs/`, and `patch`/`summary` land under `s3://sandbox-out/`.
The runner writes `session.json` (the turn ledger) and `repo.bundle` directly
under `session` — neither is an envelope, so neither passes through
`orchestrator.read_envelope`.

## Failures that are data and failures that are bugs

`run_step` runs one spec with `check=False` and reads what comes back in a
fixed order. A `lost` status — job tracking on the VM lost the process,
distinct from the whole-VM loss chapters 02 and 03 cover — makes it raise
`LeaseLost` so the caller leases another VM. Any other non-`exited` status
(`timed_out`, `cancelled`) raises `ExecFailed`: the runner never wrote a
trustworthy envelope. A non-zero exit gets one chance to read the envelope: a
readable `ok: false` one raises `StepBroken` (an `ApplicationError`) with the
runner's curated error, an unreadable one raises `ExecFailed` with the
stderr tail. A zero exit still needs an `ok: true` envelope (retried up to
five times) or it is `StepBroken` too — lint findings and failing tests are
`ok: true` data, so `ok: false` on a clean exit means the step lied.

`ExecFailed` and `LeaseLost` are `SandboxError` subclasses
(`sandbox/contract/errors.py`); `StepBroken` is only a type string `run_step`
raises directly. The workflow catches just `LeaseLost`, retrying up to
`max_lease_attempts` times; exhausting that raises `SessionAbandoned`.
Everything else fails the workflow —
`test_a_broken_step_fails_the_session_with_the_runners_error` points `clone`
at a missing repo and gets `StepBroken` with "no seed repository" in its
message.

## Losing the VM mid-turn

`session.py`'s docstring names the ordering that makes a session resumable:
"the bundle is written before the state, and `run_turn` looks for a commit
the state does not know about, so a crash between the two writes recovers
instead of redoing the turn." `run_turn` commits the edit, pushes
`repo.bundle`, and only then rewrites `session.json`; a crash in between
still leaves the commit inside the bundle, and the next `run_turn` checks
`git log --grep '^turn <n>:'` before asking the agent to redo work it already
did.

Losing the whole VM is a coarser version of the same recovery: the turn
itself never finished, so the next lease's `clone` reports the *previous*
completed turn, and the workflow verifies that turn's lint and test before
adding a new one. `test_losing_the_vm_mid_turn_resumes_from_the_bundle`
proves it: crashing the VM mid turn-2 still finishes the workflow with
`attempts == 2`, `turns == 2`, tests passing, and `vm_ids` naming the crashed
VM first — the second clone envelope reports `source: "bundle"`, `turn: 1`,
so only the missing turn is redone.

## The runner artifact

`sandbox.runner` ships to a VM as `runner-<version>-<sha12>.tar.gz`, built by
`package.py`'s `build`: the runner package plus its two shared modules,
packed with a zeroed mtime and uid/gid so identical source always produces
identical bytes — a rebuild is a cache hit everywhere. The tar's sha256
names the file and is what `ensure_artifact` verifies on the VM (chapter 04).
Its `bin/runner` launcher puts the artifact's own directory first on
`PYTHONPATH`, so, in its own words, "the version the orchestrator shipped is
the version that runs, not whatever the image was built with."

Two environment variables exist only for tests: `RUNNER_PYTHON` overrides the
interpreter `bin/runner` execs, and `SEED_REPOS_DIR` is `clone`'s
`--seed-root` default instead of `/srv/repos`. `CodingSessionParams.runner_env`
sets both in integration tests, so the packaged artifact also runs as a real
subprocess in CI.

## The fake agent

A turn's scenario comes from `--scenario` or a keyword in the prompt
(`pick_scenario`), most specific keyword first.

| Scenario | Turn 1 | Turn 2 | Teaches |
|---|---|---|---|
| `multiply-with-bug` (default) | adds `multiply` with `+`; tests fail | fixes the operator to `*`; tests pass | the fix loop |
| `divide-clean` | adds `divide` with a zero guard; tests pass | — | the happy path |
| `lint-only` | adds `power`, plus an unused `import os`; one ruff finding | — | lint findings are data |
| `never-fixes` | same as `multiply-with-bug`'s turn 1 | leaves a comment instead of fixing it | `max_turns` exhaustion |

Past a scenario's own turns, `Scenario.turn_for` repeats the last one, which
is how `never-fixes` exhausts `max_turns` rather than running out of script.

Each turn is a tuple of `read`, `think`, `edit`, and `run` steps; an `edit`
carries old text and new text, applied by `apply_edit`, which checks
"already applied" first — a repeated turn whose new text already contains
its old text would otherwise duplicate the function — and raises on a
missing target, ending the turn as broken. Each step prints a line —
`[think] ...`, `[tool] Edit calc/__init__.py (+6 -0)`, `[feedback] ...` for a
prior failure — the same lines the dashboard's log tail and `exec_wait`'s
heartbeats show live. Each step kind costs a fixed amount (`read` 0.002,
`think` 0.010, `edit` 0.020, `run` 0.005), summed into `fake_cost_usd` so the
summary shows a spend the way a production agent's cost events do.

## Real mode

`--agent claude` on `run_session.py` (not wired into `make session`) threads
through to the runner's `turn` subcommand, where `run_turn` calls
`agent.run_claude_turn` instead of the scripted path: it shells out to
`claude -p` with the prompt and any failing feedback appended, prints
`[claude] ...` lines instead of `[tool]` ones, and reports `fake_cost_usd` as
`0.0`. It checks for the `claude` binary with `shutil.which` first; this
repository's image installs no such layer today, so real mode currently
fails with "claude CLI is not installed in this image." Otherwise, per
`agent.py`'s docstring, "the orchestrator does not care which" agent ran —
clone, lint, test, the loop, and export are unchanged.

## Read the code

- `sandbox/orchestrator/workflows.py` — `CodingSessionDemoWorkflow`,
  `CodingSessionParams`, `CodingSessionResult`.
- `sandbox/orchestrator/steps.py` — `SessionUris`, `run_step`, `job_id`, the
  `ExecSpec` builders.
- `sandbox/orchestrator/activities.py` — `read_envelope`, `publish_summary`.
- `sandbox/orchestrator/run_session.py` — the `make session`/`make demo` entry.
- `sandbox/contract/errors.py` — `SandboxError`, `LeaseLost`, `ExecFailed`.
- `sandbox/runner/cli.py` — the five subcommands, the one non-zero exit code.
- `sandbox/runner/session.py` — `clone`, `run_turn`, `export`, `SessionState`.
- `sandbox/runner/agent.py`, `scenarios.py` — `run_fake_turn`,
  `run_claude_turn`, `apply_edit`, the four `Scenario` definitions.
- `sandbox/runner/checks.py`, `envelopes.py` — `run_ruff`, `run_pytest`, the
  envelope caps.
- `sandbox/runner/package.py` — `build`, `publish`, the `bin/runner` launcher.
- `tests/integration/test_session.py` — the fix loop, resume, and broken-step
  tests.

## Where this maps in production

`workflows.py` names its real callers directly: the production coding-agent workflows. `steps.py`, `checks.py`, and `session.py` each call
themselves identical in production, over real S3 instead of moto. `package.py`
says the artifact changes shape: today it carries only the runner because the
image already has its dependencies; in production it is a per-commit
virtualenv holding the real harness, built in CI, the image supplying only
the interpreter.

## Try it

Run `make session SCENARIO=multiply-with-bug TURN_SECONDS=60`, then find the
leased VM's id in `make show` while turn 2 is running and run
`make chaos-kill VM=<that id>` mid-turn. `make show` prints the same
`write_off_stopped` and replacement `launch` events as chapter 05; the
session workflow retries onto the new VM, and its printed result shows two
lease attempts and two different VM ids, with the same turn count and
passing tests as an uninterrupted run.

Next: [07 · Watching it: the status API and dashboard](07-dashboard.md)
