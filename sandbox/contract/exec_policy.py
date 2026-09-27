"""What argv shapes a workflow may ask a VM to run.

What: a small, ordered allow-rule set. `ExecPolicy.check` is called twice for
every exec: once by `Lease.exec_start` before the activity is even sent, and
again by the VM agent's `exec_start` before argv reaches sudo — so neither
side has to trust the other's validation.
Why: `ExecSpec.argv` is free-form strings; nothing else in the contract
constrains what a workflow can make a VM run. This is independent of the pool
policy (min_idle/max) in `sandbox.manager.reconciler.core` — that one bounds how
many VMs exist, this one bounds what runs on them. `DEMO_POLICY` allowlists
the exec shapes this demo's own workflows actually send today: the coding
session's and the Go build demo's `bin/runner` steps, `SmokeWorkflow`'s `uname`/`id`, and
`HoldWorkflow`'s `sleep <seconds>` — not a speculative "known safe commands"
list, the real repertoire.
Production: the same rule shapes seed the VM image's sudoers `Cmnd_Alias`
(`images/vm/Dockerfile`), or this module is swapped for whatever policy
service production standardizes on — every call site only ever calls
`.check()`, so the swap touches this file alone.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from sandbox.contract.errors import Incompatible

# sandbox/orchestrator/steps.py builds every coding-session exec as
# ["/bin/sh", "<content-addressed artifact path>/bin/runner", step, ...],
# where the artifact path is `ArtifactCache.ensure`'s `root / sha256`
# (sandbox/vm_agent/artifacts.py). `root` is deployment-specific — the real
# image uses ARTIFACTS_ROOT, the in-process test harness a per-test tmp dir —
# so the check is on the digest shape right before /bin/runner, not on a
# hardcoded root: a bare suffix/prefix match would let
# ".../artifacts/../../tmp/x/bin/runner" through as plain strings, but no
# non-digest segment (a traversal, an attacker-planted directory name) can
# satisfy "exactly 64 hex characters". A new step adds a name to
# RUNNER_STEPS; it does not touch exec_start or the client.
RUNNER_STEPS = ("clone", "turn", "lint", "test", "export", "fetch", "edit", "build", "run")
_RUNNER_PATH = re.compile(r"^/.*/[0-9a-f]{64}/bin/runner$")


@dataclass(frozen=True)
class ExecRule:
    name: str
    matches: Callable[[list[str]], bool]


@dataclass(frozen=True)
class ExecPolicy:
    rules: tuple[ExecRule, ...]

    def check(self, argv: list[str]) -> None:
        if not argv:
            raise Incompatible("argv is empty")
        if not any(rule.matches(argv) for rule in self.rules):
            raise Incompatible(f"argv {argv!r} matches no allowed exec rule")


def _runs_runner_step(step: str) -> Callable[[list[str]], bool]:
    def matches(argv: list[str]) -> bool:
        return (
            len(argv) >= 3
            and argv[0] == "/bin/sh"
            and bool(_RUNNER_PATH.match(argv[1]))
            and argv[2] == step
        )

    return matches


def _exact(*want: str) -> Callable[[list[str]], bool]:
    return lambda argv: list(argv) == list(want)


def _is_sleep(argv: list[str]) -> bool:
    # HoldWorkflow's `sleep <seconds>`: seconds is caller-supplied (`make hold
    # SECONDS=`), so the count is checked, not the value.
    return len(argv) == 2 and argv[0] == "sleep" and argv[1].isdigit()


RUNNER_RULES = tuple(ExecRule(f"runner:{step}", _runs_runner_step(step)) for step in RUNNER_STEPS)
SMOKE_RULES = (
    ExecRule("smoke:uname", _exact("uname", "-a")),
    ExecRule("smoke:whoami", _exact("id")),
)
HOLD_RULES = (ExecRule("hold:sleep", _is_sleep),)

DEMO_POLICY = ExecPolicy(rules=RUNNER_RULES + SMOKE_RULES + HOLD_RULES)

# For test doubles that intentionally run arbitrary, throwaway commands to
# exercise generic exec plumbing (heartbeat, cancel, drain) rather than
# simulate a real workflow's argv. The in-process VM harness defaults to this
# the same way it already skips sudo (`run_as_user=None`); nothing that
# reaches a real VM's sudo boundary should ever use it.
ALLOW_ALL = ExecPolicy(rules=(ExecRule("allow-all", lambda argv: True),))
