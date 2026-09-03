"""Drain state shared between the signal handler and the activities.

What: one flag.
Why: an in-flight exec_wait must be able to tell a drain (kill the job, fail
fast so the orchestrator re-dispatches) from an ordinary cancel (kill the job,
report cancelled) from a worker restart (leave the job alone, reattach later).
Production: set by the IMDS lifecycle watcher instead of SIGTERM.
"""


class DrainState:
    def __init__(self) -> None:
        self.draining = False
