# 05 · The provider: where VMs come from

After this chapter you can name the six calls any pool provider must
implement, trace each one to the Apple `container` subcommand it shells out
to, explain why moto runs on the host instead of in a container of its own,
and say which reconciler outcome each chaos action is built to prove.

## The interface

`sandbox/manager/providers/base.py` defines the whole seam between the
manager and whatever backs a VM: a `PoolProvider` Protocol —
`launch(vm_id, spec, env) -> str`, `terminate(provider_ref)`, `list()`,
`describe(provider_ref)`, `kill(provider_ref)`, `stop(provider_ref)` — plus
two frozen value types. `LaunchSpec` carries `image`, `cpus` (default `2`),
`memory` (default `"2048M"`); `ProviderInstance` carries `provider_ref`,
`vm_id`, `state`, `created_at`, `address`, `gateway`. The module's own
reasoning: "the manager's lifecycle logic must not know which compute backs
a VM. Every backend difference — an Apple container, an EC2 instance, a
hosted sandbox — stops at this interface, so the manager keeps one
implementation of leasing."

## Apple container

`AppleContainerProvider`, in `sandbox/manager/providers/apple_container.py`,
implements the protocol by shelling out to the `container` CLI and parsing
its JSON, through one `run_cli` helper every method shares. `launch` runs
`container run --detach --init --name <vm_id> --cpus N --memory M --env
K=V... <image>` and returns `vm_id` as the `provider_ref` — the container
name doubles as the id, so the manager knows it before the VM ever
registers itself. The `--init` flag is what gives the container the init
process chapter 04 named as the thing that reaps zombies and turns
`SIGTERM` into a drain. `terminate` runs `container delete --force`, but
swallows a `ProviderError` if `describe` afterward finds the instance
already gone — a second terminate on an already-gone VM must not fail the
caller. `kill` runs `container kill --signal KILL`; `stop` runs `container
stop --time 20`. `list` runs `container ls --all --format json` and keeps
only names starting with `NAME_PREFIX = "sbx-"`; `describe` runs `container
inspect` and returns `None` on any `ProviderError` rather than raising.
Every call carries its own timeout — `CLI_TIMEOUT_SECONDS = 60` for
everything except `launch`, which gets `LAUNCH_TIMEOUT_SECONDS = 180`
because it also pulls and boots an image — and a timeout or a non-zero exit
both raise `ProviderError`, the module's own `RuntimeError` subclass, so a
wedged `container` daemon cannot hold a manager activity thread forever.

## Networking on this laptop

`scripts/up.sh` starts the Temporal dev server, a host `moto_server`
standing in for DynamoDB and S3, and writes the `.env` file every host
process and every launched VM reads its endpoints from. Its own comment
explains why moto runs on the host rather than in a container: "on a
managed laptop, host processes are confined to loopback, so the `container`
bridge network is unreachable from here even though a VM can reach a host
listener over that same bridge." `up.sh` sets the bridge gateway to a
hard-coded default (`192.168.64.1`), overridable with `SANDBOX_VM_GATEWAY`
if that address ever stops matching this machine, then proves the path
works by running a throwaway alpine container that fetches from moto
through that gateway before it writes `.env`.

That file carries two forms of each endpoint: `TEMPORAL_ADDRESS` and
`DYNAMODB_ENDPOINT`/`S3_ENDPOINT` on loopback for host processes, and
`VM_TEMPORAL_ADDRESS`/`VM_DYNAMODB_ENDPOINT`/`VM_S3_ENDPOINT` on the gateway
for VMs. `vm_environment`, in `sandbox/manager/launch.py`, is what turns
those into a launched VM's actual environment: it reads `VM_TEMPORAL_ADDRESS`
as the VM's `TEMPORAL_ADDRESS`, prefers `VM_DYNAMODB_ENDPOINT`/
`VM_S3_ENDPOINT` and falls back to the host forms, and sets
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_DEFAULT_REGION` — the same
keys chapter 04 called `_VM_IDENTITY_KEYS` and showed the VM agent refuses
to let a job's own request override.

## The fake provider

`FakeProvider`, in `sandbox/manager/providers/fake.py`, backs a VM with an
in-process `InProcessVm` instead of a container, so the reconciler's whole
loop runs against a real Temporal dev server and a real `AgentRuntime` in a
test that takes seconds. `kill` and `stop` both route through `_park`,
which pops the VM out of `self.vms` and into `self.stopped`, then runs
`vm.runtime.crash()` or `vm.runtime.drain()` — the same two `AgentRuntime`
methods a real crash or a real drain would reach. A killed or stopped VM
stays in `list()`/`describe()` as `"stopped"` rather than disappearing,
because, as the module says, "a killed instance lingers in `stopped` for
the same reason a crashed EC2 instance keeps answering DescribeInstances:
only a terminate removes it, and the reconciler has to be able to tell
'stopped' from 'gone'." Only `terminate` removes it from both dicts. This
provider is test-only: "never used" in production, where "the EC2 provider
takes this seat."

## Chaos

`sandbox/manager/chaos.py`'s `apply(provider, registry, action, vm_id)` is
one dispatch table keyed by action name — `"kill"` to `provider.kill`,
`"stop"` to `provider.stop`, `"delete"` to `provider.terminate` — called
with `vm_id`, followed by `registry.emit("chaos", "chaos", ...)`, so every
chaos action leaves the same event trail a real failure would. It is shared code: both `make
chaos-kill`/`chaos-stop`/`chaos-delete` and the dashboard's chaos endpoint
call this one function. Each action reaches the reconciler by a different
path traced in `sandbox/manager/reconciler.py`. `kill` sends `container
kill --signal KILL`; the container then reports a state inside
`DEAD_STATES`, so `health` matches its `provider_ref` in `stopped_refs` and
calls `_write_off(vm_id, "instance stopped", "write_off_stopped", ...)`
before `capacity` launches a replacement. `delete` runs `container delete
--force`, which removes the container from `container ls --all` entirely;
`health` finds the row's ref in neither `live_refs` nor `stopped_refs` and
calls `_write_off(vm_id, "instance missing", "write_off_missing", ...)`
instead. `stop` sends `container stop --time 20`, a plain `SIGTERM` the VM
agent itself receives and turns into the drain chapter 04 walked through —
the row reaches `terminated` from inside the VM, before any reconciler pass
has to notice at all.

## Read the code

- `sandbox/manager/providers/base.py` — `PoolProvider`, `LaunchSpec`,
  `ProviderInstance`.
- `sandbox/manager/providers/apple_container.py` — `AppleContainerProvider`,
  `run_cli`, `NAME_PREFIX`, `CLI_TIMEOUT_SECONDS`,
  `LAUNCH_TIMEOUT_SECONDS`, `ProviderError`.
- `sandbox/manager/providers/fake.py` — `FakeProvider`, `_park`,
  `InProcessVm`.
- `sandbox/manager/launch.py` — `new_vm_id`, `launch_spec`,
  `vm_environment`.
- `sandbox/manager/launch_vm.py` — the manual, one-VM launcher.
- `sandbox/manager/chaos.py` — `apply`, `ACTIONS`.
- `scripts/up.sh` — the gateway discovery and the `.env` it writes.
- `scripts/build-image.sh` — the lockfile export `container build` installs
  from.

## Where this maps in production

`base.py` names its successor directly: "an EC2 provider over the ASG and
EC2 APIs." `apple_container.py` is explicit about the mapping: "replaced by
an EC2 provider over RunInstances, TerminateInstances, DescribeInstances,
and ASG instance protection. Nothing above this module changes." Instance
protection itself has no local equivalent — it is a registry flag
(`protected`) the reconciler's `capacity` step already honors, backed in
production by real ASG instance protection. `launch.py`'s own docstring
says its production form is "user_data written by the EC2 provider from
the same function" — the same `vm_environment` output, landing in EC2
`user_data` instead of a `container run --env` flag. Spec §8.3 records the
one contingency this laptop setup carries: if the container bridge ever
stops reaching a host listener the way it does today, the documented
fallback is an `ExecTransport` inside the provider driving `container exec`
directly, not a silent behavior change.

## Try it

Find an idle VM's id in `make show`, then run `make chaos-kill VM=<that
id>`. Watch `make show`: a `write_off_stopped` event appears once the next
reconciler pass's health step sees the container's dead state, followed by
a `launch` event as capacity replaces it.

Next: [06 · A coding session: the orchestrator and the fake agent](06-orchestrator-and-fake-agent.md)
