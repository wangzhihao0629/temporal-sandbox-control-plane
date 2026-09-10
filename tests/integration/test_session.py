# tests/integration/test_session.py
"""CodingSessionDemoWorkflow end to end: the fix loop, the clean path, lint as data,
max_turns exhaustion, and resuming from the bundle after the VM dies mid-turn."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio.worker import Worker

from sandbox.contract.names import MANAGER_TASK_QUEUE, ORCHESTRATOR_TASK_QUEUE
from sandbox.manager.activities import ManagerActivities
from sandbox.orchestrator.activities import OrchestratorActivities
from sandbox.orchestrator.steps import SessionUris
from sandbox.orchestrator.workflows import CodingSessionDemoWorkflow
from sandbox.runner.cli import main
from sandbox.testing.inprocess_vm import InProcessVm
from sandbox.testing.stubs import StubProvider
from tests.integration.conftest import session_params


def _seed_session(store, sid, seed_root, tmp_path, scenario, turns):
    """Pre-populate a session's bundle and state by running the runner CLI
    in-process, as if a previous lease had already completed some turns."""
    uris = SessionUris(sid)
    ws = tmp_path / f"seed-{sid}"
    assert (
        main(
            [
                "clone",
                "--repo",
                "hello",
                "--workspace",
                str(ws),
                "--session-uri",
                uris.session,
                "--seed-root",
                str(seed_root),
                "--envelope-uri",
                f"{uris.session}/steps/seed-clone.json",
            ]
        )
        == 0
    )
    for n in range(1, turns + 1):
        assert (
            main(
                [
                    "turn",
                    "--workspace",
                    str(ws),
                    "--session-uri",
                    uris.session,
                    "--prompt",
                    "x",
                    "--scenario",
                    scenario,
                    "--feedback-uri",
                    "none",
                    "--seconds",
                    "0",
                    "--envelope-uri",
                    f"{uris.session}/steps/seed-turn-{n}.json",
                ]
            )
            == 0
        )


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


async def test_a_clean_scenario_takes_one_turn(
    env, aws_server, session_workers, vm, runner_artifact, seed_root, tmp_path
):
    sid = f"clean-{uuid.uuid4().hex[:6]}"
    result = await _run(
        env,
        session_params(sid, runner_artifact, seed_root, tmp_path / "ws", scenario="divide-clean"),
    )
    assert result.turns == 1 and result.tests_passed and result.lint_findings == 0
    _, store = aws_server
    assert "def divide" in store.get_bytes(SessionUris(sid).patch).decode()


async def test_lint_findings_are_data_not_failures(
    env, aws_server, session_workers, vm, runner_artifact, seed_root, tmp_path
):
    sid = f"lint-{uuid.uuid4().hex[:6]}"
    result = await _run(
        env, session_params(sid, runner_artifact, seed_root, tmp_path / "ws", scenario="lint-only")
    )
    assert result.turns == 1 and result.tests_passed and result.lint_findings == 1
    _, store = aws_server
    lint = store.get_json(SessionUris(sid).envelope(f"{sid}-lint-t1-a1"))
    assert lint["findings"][0]["code"] == "F401"


async def test_max_turns_exhaustion_is_a_result_not_an_error(
    env, aws_server, session_workers, vm, runner_artifact, seed_root, tmp_path
):
    sid = f"never-{uuid.uuid4().hex[:6]}"
    result = await _run(
        env,
        session_params(
            sid, runner_artifact, seed_root, tmp_path / "ws", scenario="never-fixes", max_turns=2
        ),
    )
    assert result.turns == 2 and not result.tests_passed and result.tests_failed == 1
    _, store = aws_server
    summary = store.get_json(result.summary_uri)
    assert summary["tests_passed"] is False and summary["turns"] == 2
    assert store.exists(SessionUris(sid).patch), "exported anyway"


async def test_a_resumed_session_verifies_the_restored_turn_before_adding_one(
    env, aws_server, session_workers, vm, runner_artifact, seed_root, tmp_path
):
    _, store = aws_server
    sid = f"resume-verify-{uuid.uuid4().hex[:6]}"
    _seed_session(store, sid, seed_root, tmp_path, "never-fixes", 2)
    result = await _run(
        env,
        session_params(
            sid, runner_artifact, seed_root, tmp_path / "ws", scenario="never-fixes", max_turns=2
        ),
    )
    assert result.turns == 2 and result.tests_passed is False and result.attempts == 1
    uris = SessionUris(sid)
    assert store.exists(uris.envelope(f"{sid}-lint-t2-a1"))
    assert store.exists(uris.envelope(f"{sid}-test-t2-a1"))
    assert not store.exists(uris.envelope(f"{sid}-turn-t3-a1"))
    summary = store.get_json(result.summary_uri)
    assert summary["scenario"] == "never-fixes"


async def test_a_resumed_session_whose_turn_already_passed_runs_no_new_turn(
    env, aws_server, session_workers, vm, runner_artifact, seed_root, tmp_path
):
    _, store = aws_server
    sid = f"resume-done-{uuid.uuid4().hex[:6]}"
    _seed_session(store, sid, seed_root, tmp_path, "multiply-with-bug", 2)
    result = await _run(
        env,
        session_params(
            sid, runner_artifact, seed_root, tmp_path / "ws", scenario="multiply-with-bug"
        ),
    )
    assert result.turns == 2 and result.tests_passed
    uris = SessionUris(sid)
    assert not store.exists(uris.envelope(f"{sid}-turn-t3-a1"))
    patch = store.get_bytes(uris.patch).decode()
    assert "+    return a * b" in patch


async def _wait_for_running_job(registry, vm_id, job_id, seconds=30):
    for _ in range(int(seconds / 0.1)):
        for row in registry.list_jobs(vm_id):
            if row["job_id"] == job_id and row["status"] == "running":
                return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{job_id} never ran on {vm_id}")


async def test_losing_the_vm_mid_turn_resumes_from_the_bundle(
    env, aws_server, session_workers, runner_artifact, seed_root, tmp_path
):
    registry, store = aws_server
    root = tmp_path / "ws"
    vms = {}
    for name in ("sbx-a", "sbx-b"):
        vms[name] = await InProcessVm(
            env.client, registry, store, tmp_path, vm_id=name, workspace_root=root
        ).start()
    sid = f"lost-{uuid.uuid4().hex[:6]}"
    params = session_params(
        sid, runner_artifact, seed_root, root, scenario="multiply-with-bug", turn_seconds=4
    )
    victim = None
    try:
        handle = await env.client.start_workflow(
            CodingSessionDemoWorkflow.run,
            params,
            id=f"session-{sid}",
            task_queue=ORCHESTRATOR_TASK_QUEUE,
        )
        # Wait until turn 2 is running on whichever VM took the lease, then kill it.
        for _ in range(600):
            leased = [r for r in registry.list_vms("demo") if r["state"] == "leased"]
            if leased:
                break
            await asyncio.sleep(0.1)
        victim = leased[0]["vm_id"]
        await _wait_for_running_job(registry, victim, f"{sid}-turn-t2-a1")
        await asyncio.sleep(0.5)
        await vms[victim].runtime.crash()

        result = await asyncio.wait_for(handle.result(), timeout=120)
        assert result.attempts == 2 and result.turns == 2 and result.tests_passed
        assert result.vm_ids[0] == victim and result.vm_ids[1] != victim
        uris = SessionUris(sid)
        clone2 = store.get_json(uris.envelope(f"{sid}-clone-t0-a2"))
        assert clone2["source"] == "bundle" and clone2["turn"] == 1
        state = store.get_json(f"{uris.session}/session.json")
        assert state["turn"] == 2 and [h["turn"] for h in state["history"]] == [1, 2]
    finally:
        for vm in vms.values():
            if vm.vm_id != victim:
                await vm.stop()
