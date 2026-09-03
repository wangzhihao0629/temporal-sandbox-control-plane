"""Timeout profiles.

What: every duration the client hands to Temporal, in one frozen object with
local, production, and test presets.
Why: liveness is Temporal's job. Schedule-to-start says how long an idle VM
queue may go unanswered before the VM is presumed dead; heartbeat says how long
a running job may go silent. Tests want seconds, production wants minutes, and
workflow code must not read the environment to decide.
Production: `Timeouts.prod()`.
"""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class Timeouts:
    schedule_to_start: timedelta
    heartbeat: timedelta
    short: timedelta = timedelta(minutes=1)
    file_transfer: timedelta = timedelta(minutes=15)
    wait_slack: timedelta = timedelta(minutes=5)
    acquire_retry_delay: timedelta = timedelta(seconds=5)
    acquire_wait: timedelta = timedelta(minutes=10)

    @classmethod
    def local(cls) -> "Timeouts":
        return cls(schedule_to_start=timedelta(seconds=30), heartbeat=timedelta(seconds=30))

    @classmethod
    def prod(cls) -> "Timeouts":
        return cls(
            schedule_to_start=timedelta(minutes=5),
            heartbeat=timedelta(minutes=2),
            acquire_retry_delay=timedelta(seconds=30),
        )

    @classmethod
    def test(cls) -> "Timeouts":
        return cls(
            schedule_to_start=timedelta(seconds=3),
            heartbeat=timedelta(seconds=5),
            wait_slack=timedelta(seconds=10),
            acquire_retry_delay=timedelta(milliseconds=500),
            acquire_wait=timedelta(seconds=5),
        )

    @classmethod
    def for_profile(cls, name: str) -> "Timeouts":
        return {"local": cls.local, "prod": cls.prod, "test": cls.test}[name]()
