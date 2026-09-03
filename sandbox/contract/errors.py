"""Error types that cross the boundary.

What: ApplicationError subclasses with stable `type` strings, plus the strings
themselves for code that only sees the wire form.
Why: a Python class does not survive serialization, its `type` string does.
The client maps failures back to these classes so workflow code can catch
`LeaseLost` instead of inspecting Temporal failure chains.
Production: identical.
"""

from datetime import timedelta

from temporalio.exceptions import ApplicationError

NO_CAPACITY = "NoCapacity"
LEASE_LOST = "LeaseLost"
INCOMPATIBLE = "Incompatible"
EXEC_FAILED = "ExecFailed"
HOST_DRAINING = "HostDraining"
SANDBOX_UNAVAILABLE = "SandboxUnavailable"


class SandboxError(ApplicationError):
    TYPE = "SandboxError"

    def __init__(
        self,
        message: str,
        *details,
        non_retryable: bool = True,
        next_retry_delay: timedelta | None = None,
    ) -> None:
        super().__init__(
            message,
            *details,
            type=self.TYPE,
            non_retryable=non_retryable,
            next_retry_delay=next_retry_delay,
        )


class NoCapacity(SandboxError):
    TYPE = NO_CAPACITY


class LeaseLost(SandboxError):
    TYPE = LEASE_LOST


class Incompatible(SandboxError):
    TYPE = INCOMPATIBLE


class SandboxUnavailable(SandboxError):
    TYPE = SANDBOX_UNAVAILABLE


class HostDraining(SandboxError):
    TYPE = HOST_DRAINING

    def __init__(self, message: str = "host is draining") -> None:
        super().__init__(message, non_retryable=False, next_retry_delay=timedelta(seconds=1))


class ExecFailed(SandboxError):
    TYPE = EXEC_FAILED

    def __init__(self, result) -> None:
        tail = result.stderr_tail[-500:] or result.stdout_tail[-500:]
        super().__init__(
            f"job {result.job_id} {result.status} exit={result.exit_code}: {tail}",
            result,
        )
        self.result = result
