"""Where a job may connect to: an egress allowlist for the job user.

What: `NetworkPolicy` names the hosts and ports a job may reach, on top of the
two things every job needs (DNS, and the object store the runner writes its
envelopes to). Everything else is refused. `DEMO_NETWORK_POLICY` allows HTTPS
to github.com, which is all the Go build demo's clone needs.
Why: the exec policy limits which commands run; without this, any of them
could still send the workspace, or a secret from its environment, to any host
on the internet. The rule binds only the job user (`agent`): the worker keeps
its own access to Temporal, the registry, and the object store. The policy is
data, so a provider that has its own egress controls (a Modal sandbox, a VPC
security group) can be handed the same object instead of nftables.
Production: an egress proxy that allowlists by hostname rather than by the IPs
a name resolved to at boot, and DNS through a resolver that only answers for
allowed names — the demo's two known gaps (see sandbox/vm_agent/egress.py).
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class NetworkPolicy:
    allow_hosts: tuple[str, ...] = ()
    allow_ports: tuple[int, ...] = (443,)

    @classmethod
    def from_env(cls, env=os.environ) -> "NetworkPolicy":
        """`SANDBOX_EGRESS_ALLOW_HOSTS=a.com,b.com` replaces the demo's host list;
        set but empty, it allows no hosts at all."""
        raw = env.get("SANDBOX_EGRESS_ALLOW_HOSTS")
        if raw is None:
            return DEMO_NETWORK_POLICY
        hosts = tuple(h.strip() for h in raw.split(",") if h.strip())
        return cls(allow_hosts=hosts, allow_ports=DEMO_NETWORK_POLICY.allow_ports)


DEMO_NETWORK_POLICY = NetworkPolicy(allow_hosts=("github.com",))
