# 10 · Snapshots, egress, and a real build

After this chapter you can run a demo that clones a real repository from
GitHub, edits it, compiles and runs it inside a VM, and restores the result
on a second VM; explain what a workspace snapshot captures and what it does
not; and say exactly which hosts a job may reach and why the rule binds the
job user alone.

## The Go build demo

`GoBuildDemoWorkflow` (`sandbox/orchestrator/gobuild_workflow.py`) is the
coding session's counterpart for a compiled language and a repository that
lives on the network, not in the image. It runs four runner steps from
`sandbox/runner/gobuild.py`, each its own exec activity with its own envelope,
the same way chapter 06's steps do:

1. `fetch` clones `https://github.com/golang/example` at a pinned commit into
   `<workspace>/repo`. It accepts `https://` only and passes git
   `protocol.allow=never` with an exception for https, so a URL cannot be a
   `file://` path to somewhere else on the VM.
2. `edit` applies one named edit from `GO_EDITS` — `greet-sandbox` changes
   `name := "world"` to `name := "Temporal sandbox"` in `hello/hello.go`,
   using the same `apply_edit` the fake agent uses.
3. `build` runs `go build -buildvcs=false` with `GOPROXY=off` and
   `GOTOOLCHAIN=local`, so the build downloads nothing: the `hello` module
   imports only its own `reverse` package. The binary goes to `repo/bin/app`;
   the build cache goes beside `repo/`, not in it.
4. `run` executes the binary and reports its stdout and exit code as data.

The workflow then snapshots `repo/`, and — still holding the first lease —
acquires a second VM. Holding the first lease is what makes the second a
different machine rather than the first one recycled. It restores the
snapshot there and runs the restored binary without building. The two runs
printing the same line is the proof that the snapshot carried both the edit
and the build output.

The four new step names are added to `RUNNER_STEPS` in
`sandbox/contract/exec_policy.py`, and nothing else in the exec policy or the
sudoers rule changes: `git` and `go` run inside the runner, so the only argv
that crosses the boundary is still `/bin/sh .../bin/runner <step>`.

## When a VM dies

The two phases retry separately through `Sandbox.with_lease_retries`
(`sandbox/client/sandbox.py`), each up to `max_lease_attempts` leases, and each
attempt works in a directory of its own so it never finds a half-finished
clone from the one before. Losing the build VM before the snapshot exists
starts the build over on a fresh VM: nothing from it survived. Losing the
restore VM repeats only the restore, on another VM, because the snapshot is
already in the object store — the point of taking one. Losing the build VM
after its snapshot changes nothing, since nothing calls it again. Running out
of attempts fails with `LeaseAttemptsExhausted` naming the VMs lost, never a
bare `LeaseLost`. `fetch` also retries git's own network failures (a DNS
miss, a reset connection) twice with backoff, and fails at once on anything
permanent, such as a repository that does not exist.

`tests/integration/test_gobuild.py` covers each case on in-process VMs:
crashing the build VM mid-build, a dead VM handed to the restore phase, and
running out of restore attempts.

Running those tests repeatedly found a platform bug: a late heartbeat
cancelled `exec_wait`, the agent killed the job, and the retry reattached to a
dead job. A
heartbeat timeout and a workflow cancel both reached the VM as the same
`not_found`, which is why the agent could not tell them apart. So the agent no
longer decides: `exec_wait` only observes a job and never stops it when
cancelled, and a retry reattaches. When the workflow itself is cancelled, the
client calls `exec_cancel`, which stops the job and flushes its logs. `test_a_missed_heartbeat_leaves_the_job_running_for_the_retry` in
`tests/integration/test_vm_agent.py` reproduces it deterministically.

## Snapshots

`snapshot` and `restore` are the two newest `VM_OPERATIONS`
(`sandbox/contract/names.py`), a minor contract bump to `1.1` because they
only add. `sandbox/vm_agent/snapshots.py` does the work:

- `snapshot` tars a directory under the workspace root, uploads it to the
  object store, and returns a `SnapshotRef` with its sha256, size, and file
  count. The worker reads what jobs wrote through its `agent` group
  membership.
- `restore` downloads a snapshot, refuses it if the digest does not match,
  and unpacks it with `tarfile`'s `data` filter, which refuses members that
  escape the target directory. It refuses a target that already exists,
  unpacks into a staging directory and renames it into place, and makes the
  tree group-writable so the next job, which runs as `agent`, can build in
  it.

This is a portable snapshot: it needs nothing from the provider, so it works
the same on Apple `container` today and on Modal or EC2 later. It captures
files — source, build output — and not running processes, installed
packages, or anything outside the directory. A provider that can snapshot a
whole machine would offer that as an optional extra on `PoolProvider`, not
instead: Apple `container` has `container export` for a filesystem but no
matching restore, and Modal has its own sandbox snapshots.

## Egress: what a job may connect to

The exec policy limits which commands run. Without a network rule, any of
them could still send the workspace, or a secret from its environment, to
any host. `sandbox/contract/network_policy.py` holds `NetworkPolicy`, shaped
like `ExecPolicy`: `DEMO_NETWORK_POLICY` allows HTTPS to `github.com` and
nothing else, and `SANDBOX_EGRESS_ALLOW_HOSTS` replaces the host list.

`sandbox/vm_agent/egress.py` turns it into nftables rules at boot. The
image's entrypoint runs it as root before it drops privileges, and the
provider launches the VM with `CAP_NET_ADMIN` for exactly that step. The
rules match on the socket's uid, so they bind the job user and leave the
worker — which needs Temporal, the registry, and the object store — alone.
For `agent`, only three kinds of connection are allowed: DNS to the VM's
nameservers, the object store the runner writes envelopes to, and the
allowed hosts on the allowed ports. Everything else gets `reject`, so a
blocked connection fails at once instead of hanging.

Two properties keep the rules in place. If they cannot be applied, the
entrypoint exits and the VM never registers, so it never boots open. And
`setpriv --bounding-set=-net_admin` removes the capability for good before
the worker starts, so not even sudo's setuid step can get it back to undo
them.

Hosts are matched by the addresses they resolved to at boot, and the same
addresses are pinned in the VM's `/etc/hosts` (`pin_hosts`). Without the pin,
a job resolving `github.com` minutes later got a newer address the rules did
not allow — the demo's own chaos drill found that. Two gaps remain, deliberate
for a local demo and named in the code: a pinned address the host retires
stops working until the VM is replaced, and a job can still query DNS for any
name through the allowed resolver, a slow but real exfiltration channel.

## Read the code

- `sandbox/orchestrator/gobuild_workflow.py` — the demo, two leases, one
  snapshot, and the per-phase retries.
- `tests/integration/test_gobuild.py` — the failure cases, one test each.
- `sandbox/runner/gobuild.py` — `fetch`, `edit`, `build`, `run`, `GO_EDITS`.
- `sandbox/vm_agent/snapshots.py` — `snapshot`, `restore`.
- `sandbox/contract/network_policy.py` — `NetworkPolicy`,
  `DEMO_NETWORK_POLICY`.
- `sandbox/vm_agent/egress.py` — `render`, the ruleset; `main`, applying it.
- `images/vm/entrypoint.sh` — the order: rules as root, then the drop.
- `scripts/network-probe.sh` — what `make check-network` asserts.

## Where this maps in production

The real agent makes the edits; `fetch`, `build`, and `run` keep their shape.
Snapshots stream to the object store in parts instead of through a temp
file. Egress moves out of the VM to a proxy that allowlists by hostname, with
a resolver that answers only for allowed names, which closes both gaps above.
A provider with its own egress controls, such as a Modal sandbox or a VPC
security group, is handed the same `NetworkPolicy` instead of nftables.

## Try it

With the stack up (chapter 08), `make demo-gobuild` printed:

```
built:   sbx-07dce6e5  head 7f05d217867b  go1.22.2  2222218 bytes
ran:     Hello, Temporal sandbox!
snap:    s3://sandbox-sessions/go-7489a310/snapshots/repo.tar.gz  (149 files, 1745699 bytes)
restore: sbx-eef6ea38
ran:     Hello, Temporal sandbox!
match:   yes
```

`make demo-gobuild ARGS=-r` passes `-r` through to the program, and both VMs
printed `olleH, xobdnas laropmeT!`. Pointing the demo at a host outside the
allowlist fails in `fetch` with git's own "unable to access" error.
`make check-network` runs twelve checks on a real VM: `agent` reaches
`github.com` over HTTPS at its pinned address and resolves names, and is refused `example.com`,
`1.1.1.1`, `github.com` on port 80, and the host's ssh; root and the worker
still reach `example.com`; and neither `agent` nor a root process without
`NET_ADMIN` can change the rules.

Back to the [index](../README.md)
