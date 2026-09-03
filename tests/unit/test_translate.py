"""Translation of Temporal activity failures into contract errors."""

from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError, TimeoutType

from sandbox.client.translate import translate
from sandbox.contract import errors


def _wrap(cause):
    err = ActivityError(
        "activity failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="w",
        activity_type="sandbox.v1.exec_wait",
        activity_id="1",
        retry_state=None,
    )
    err.__cause__ = cause
    return err


def test_vm_timeouts_become_lease_lost():
    for kind in (TimeoutType.SCHEDULE_TO_START, TimeoutType.HEARTBEAT, TimeoutType.START_TO_CLOSE):
        failure = TimeoutError("t", type=kind, last_heartbeat_details=[])
        assert isinstance(translate(_wrap(failure), vm_call=True), errors.LeaseLost)


def test_manager_timeouts_are_not_lease_lost():
    mapped = translate(
        _wrap(TimeoutError("t", type=TimeoutType.START_TO_CLOSE, last_heartbeat_details=[])),
        vm_call=False,
    )
    assert mapped is None


def _app(kind, vm_call):
    return translate(_wrap(ApplicationError("x", type=kind)), vm_call=vm_call)


def test_application_error_types_map_to_client_errors():
    assert isinstance(_app("NoCapacity", vm_call=False), errors.NoCapacity)
    assert isinstance(_app("LeaseLost", vm_call=True), errors.LeaseLost)
    assert isinstance(_app("HostDraining", vm_call=True), errors.LeaseLost)
    assert isinstance(_app("Incompatible", vm_call=True), errors.Incompatible)
    assert _app("Other", vm_call=True) is None
    assert translate(_wrap(RuntimeError("boom")), vm_call=True) is None
