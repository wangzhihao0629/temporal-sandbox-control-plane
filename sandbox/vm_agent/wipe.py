"""Wipe the workspace between leases.

What: kill anything the agent user left running, empty the workspace root,
prune old job records, keep the artifact cache.
Why: the next lease must start from a clean machine, and the VM is the only
thing that can clean itself. The manager never runs commands on VMs.
Production: identical.
"""

import shutil
import subprocess

from sandbox.vm_agent.config import AgentConfig
from sandbox.vm_agent.jobs import JobStore


def wipe(cfg: AgentConfig, jobs: JobStore) -> None:
    for job_id in jobs.running_job_ids():
        jobs.cancel(job_id, grace_seconds=2, reason="cancelled")
    if cfg.run_as_user:
        subprocess.run(
            ["sudo", "-n", "-u", cfg.run_as_user, "--", "pkill", "-KILL", "-u", cfg.run_as_user],
            check=False,
        )
        subprocess.run(
            [
                "sudo",
                "-n",
                "-u",
                cfg.run_as_user,
                "--",
                "sh",
                "-c",
                f'find "{cfg.workspace_root}" -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +',
            ],
            check=False,
        )
    else:
        for child in cfg.workspace_root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    jobs.prune(older_than_seconds=3600)
