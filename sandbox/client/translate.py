"""Map Temporal failures to contract errors.

What: one function that looks at an ActivityError's cause and returns the
SandboxError a caller should see, or None to re-raise the original.
Why: a dead VM shows up as a schedule-to-start or heartbeat timeout, a
draining VM as an application error typed HostDraining, and both mean the same
thing to a caller: the lease is gone. Manager timeouts are not lease losses.
Production: identical.
"""

from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError, TimeoutType

from sandbox.contract import errors

_VM_LOST_TIMEOUTS = {
    TimeoutType.SCHEDULE_TO_START,
    TimeoutType.HEARTBEAT,
    TimeoutType.START_TO_CLOSE,
    TimeoutType.SCHEDULE_TO_CLOSE,
}


def translate(err: BaseException, vm_call: bool) -> errors.SandboxError | None:
    cause = err.cause if isinstance(err, ActivityError) else err
    if isinstance(cause, TimeoutError):
        if vm_call and cause.type in _VM_LOST_TIMEOUTS:
            return errors.LeaseLost(f"VM did not answer: {cause.type.name.lower()}")
        return None
    if isinstance(cause, ApplicationError):
        if cause.type in (errors.LEASE_LOST, errors.HOST_DRAINING):
            return errors.LeaseLost(cause.message)
        if cause.type == errors.NO_CAPACITY:
            return errors.NoCapacity(cause.message)
        if cause.type == errors.INCOMPATIBLE:
            return errors.Incompatible(cause.message)
    return None
