# tests/integration/test_session.py
"""CodingSessionDemoWorkflow end to end: the fix loop, the clean path, lint as data,
max_turns exhaustion, and resuming from the bundle after the VM dies mid-turn."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.worker import Worker

from sandbox.contract.names import MANAGER_TASK_QUEUE, ORCHESTRATOR_TASK_QUEUE
from sandbox.manager.activities import ManagerActivities
from sandbox.orchestrator.activities import OrchestratorActivities
from sandbox.orchestrator.steps import SessionUris
from sandbox.orchestrator.workflows import CodingSessionDemoWorkflow
from sandbox.testing.inprocess_vm import InProcessVm
from sandbox.testing.stubs import StubProvider
from tests.integration.conftest import session_params


@pytest.fixture
async def session_workers(env, aws_server):
    registry, store = aws_server
    async with (
        Worker(
            env.client,
            task_queue=MANAGER_TASK_QUEUE,
            activities=ManagerActivities(registry, StubProvider()).all(),
            activity_executor=ThreadPoolExecutor(4),
        ),
        Worker(
            env.client,
            task_queue=ORCHESTRATOR_TASK_QUEUE,
            workflows=[CodingSessionDemoWorkflow],
            activities=OrchestratorActivities(store).all(),
        ),
    ):
        yield


@pytest.fixture
async def vm(env, aws_server, tmp_path):
    registry, store = aws_server
    vm = InProcessVm(env.client, registry, store, tmp_path, workspace_root=tmp_path / "ws")
    await vm.start()
    yield vm
    await vm.stop()


async def _run(env, params):
    return await env.client.execute_workflow(
        CodingSessionDemoWorkflow.run,
        params,
        id=f"session-{params.session_id}",
        task_queue=ORCHESTRATOR_TASK_QUEUE,
    )


async def test_the_fix_loop_stops_on_green_tests(
    env, aws_server, session_workers, vm, runner_artifact, seed_root, tmp_path
):
    _, store = aws_server
    sid = f"fix-{uuid.uuid4().hex[:6]}"
    params = session_params(
        sid, runner_artifact, seed_root, tmp_path / "ws", scenario="multiply-with-bug"
    )
    result = await _run(env, params)
    assert result.turns == 2 and result.tests_passed and result.tests_failed == 0
    assert result.attempts == 1 and result.vm_ids == [vm.vm_id]
    assert result.lint_findings == 0 and result.fake_cost_usd > 0

    uris = SessionUris(sid)
    patch = store.get_bytes(uris.patch).decode()
    assert "[PATCH 1/2] turn 1: add multiply" in patch and "+    return a * b" in patch
    summary = store.get_json(result.summary_uri)
    assert summary["turns"] == 2 and summary["tests_passed"] and summary["finished_at"]
    assert summary["scenario"] == "multiply-with-bug" and summary["vm_ids"] == [vm.vm_id]

    turn2 = store.get_json(uris.envelope(f"{sid}-turn-t2-a1"))
    assert turn2["feedback_used"], "the second turn saw the first turn's failures"
    test1 = store.get_json(uris.envelope(f"{sid}-test-t1-a1"))
    assert test1["failed"] == 1 and test1["failures"][0]["test"].endswith("test_multiply")
    log = store.get_bytes(f"{uris.log(f'{sid}-turn-t2-a1')}/stdout.log").decode()
    assert "[feedback] 1 failing test" in log and "[tool] Edit calc/__init__.py" in log
