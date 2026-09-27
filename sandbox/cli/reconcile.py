"""Run one reconcile pass now and print what it did.

What: trigger the pool's reconcile schedule, wait for the run it starts, and
print that run's actions and counts. A missing or paused schedule is reported
rather than triggered, because neither would produce a run.
Why: a demo or a chaos drill needs the reconciler's reaction now, not after the
interval next fires. Triggering the schedule rather than starting a second
workflow keeps the schedule's overlap policy in force, so a manual pass can
never run beside the scheduled one and race it over the same fleet.
Production: not deployed; production always runs on the Schedule.

Usage: uv run python -m sandbox.cli.reconcile
"""

import asyncio
import os

from temporalio.client import Client, ScheduleOverlapPolicy
from temporalio.service import RPCError, RPCStatusCode

from sandbox import envfile
from sandbox.cli import common
from sandbox.manager.reconciler.schedule import schedule_id_for
from sandbox.manager.reconciler.types import ReconcileReport

WAIT_SECONDS = 60
POLL_SECONDS = 0.5
RESULT_SECONDS = 180


def _run_id(action) -> str:
    """The run id of a schedule action, or "" for no action at all."""
    return action.action.first_execution_run_id if action else ""


async def _latest_action(handle):
    info = (await handle.describe()).info
    return info.recent_actions[-1] if info.recent_actions else None


async def run_once(client: Client, pool: str) -> ReconcileReport:
    """Trigger the pool's schedule and return the report of the run it starts."""
    schedule_id = schedule_id_for(pool)
    handle = client.get_schedule_handle(schedule_id)
    try:
        desc = await handle.describe()
    except RPCError as e:
        if e.status != RPCStatusCode.NOT_FOUND:
            raise
        raise SystemExit(
            f"no reconcile schedule {schedule_id!r}: start the manager worker (make workers)"
        ) from None
    # A paused schedule accepts a trigger and runs nothing, so the wait below
    # would just time out. Say what is actually wrong instead.
    if desc.schedule.state.paused:
        raise SystemExit(
            f"schedule {schedule_id} is paused ({desc.schedule.state.note}); "
            "unpause it or run with SANDBOX_RECONCILE_DISABLED unset"
        )
    before = _run_id(desc.info.recent_actions[-1] if desc.info.recent_actions else None)

    await handle.trigger(overlap=ScheduleOverlapPolicy.SKIP)
    # A triggered action can be skipped by the overlap policy, so this waits for
    # the schedule to report a run rather than assuming the trigger made one.
    latest = None
    for _ in range(int(WAIT_SECONDS / POLL_SECONDS)):
        latest = await _latest_action(handle)
        if latest is not None and _run_id(latest) != before:
            break
        await asyncio.sleep(POLL_SECONDS)
    else:
        raise SystemExit(
            f"schedule {schedule_id!r} started no run within {WAIT_SECONDS}s; "
            "is the manager worker running?"
        )

    print(f"  run {latest.action.workflow_id}")
    result = client.get_workflow_handle(
        latest.action.workflow_id,
        run_id=latest.action.first_execution_run_id,
        result_type=ReconcileReport,
    ).result()
    try:
        return await asyncio.wait_for(result, timeout=RESULT_SECONDS)
    except TimeoutError:
        raise SystemExit(
            f"run {latest.action.workflow_id} was still going after {RESULT_SECONDS}s; "
            "check the manager worker and the Temporal UI"
        ) from None


async def main() -> None:
    envfile.load()
    client = await common.connect()
    report = await run_once(client, os.environ.get("SANDBOX_POOL", "demo"))
    for action in report.actions:
        print(f"  {action.kind:<20} {action.vm_id:<16} {action.detail}")
    if not report.actions:
        print("  no actions")
    print("  counts:", ", ".join(f"{k}={v}" for k, v in report.counts.items()))


if __name__ == "__main__":
    asyncio.run(main())
