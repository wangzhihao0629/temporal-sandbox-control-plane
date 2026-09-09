"""HeartbeatLoop: recycling wipes and returns to idle; written-off rows stop the loop."""

import asyncio
from pathlib import Path

from sandbox.vm_agent.config import AgentConfig
from sandbox.vm_agent.heartbeat import HeartbeatLoop


class FakeRegistry:
    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def heartbeat(self, vm_id):
        self.calls.append(("heartbeat", vm_id))
        state = self.states.pop(0)
        if isinstance(state, Exception):
            raise state
        return state

    def set_state(self, vm_id, state, *, expect=None, reason=""):
        self.calls.append(("set_state", state, expect))
        return True

    def emit(self, *args, **kwargs):
        self.calls.append(("emit", args[0]))


def _cfg(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        vm_id="sbx-t",
        pool="demo",
        provider_ref="sbx-t",
        temporal_address="",
        temporal_namespace="default",
        agent_version="test",
        jobs_dir=tmp_path / "j",
        artifacts_dir=tmp_path / "a",
        workspace_root=tmp_path / "w",
        secrets_dir=tmp_path / "s",
        run_as_user=None,
        heartbeat_seconds=0.01,
    )


async def test_recycling_triggers_wipe_then_idle(tmp_path):
    registry = FakeRegistry(["idle", "recycling"])
    wiped = []
    loop = HeartbeatLoop(
        _cfg(tmp_path), registry, wipe_fn=lambda: wiped.append(1), on_written_off=lambda: None
    )
    assert await loop.tick() == "idle" and wiped == []
    assert await loop.tick() == "idle" and wiped == [1]
    assert ("set_state", "idle", "recycling") in registry.calls


async def test_written_off_or_missing_row_stops_the_loop(tmp_path):
    for terminal in ("terminated", "dead", None):
        stopped = []
        registry = FakeRegistry([terminal])
        loop = HeartbeatLoop(
            _cfg(tmp_path),
            registry,
            wipe_fn=lambda: None,
            on_written_off=lambda stopped=stopped: stopped.append(1),
        )
        assert await loop.tick() is None and stopped == [1]


async def test_run_survives_a_failing_tick_and_ends_on_write_off(tmp_path):
    registry = FakeRegistry([RuntimeError("blip"), "idle", "terminated"])
    stopped = []
    loop = HeartbeatLoop(
        _cfg(tmp_path),
        registry,
        wipe_fn=lambda: None,
        on_written_off=lambda: stopped.append(1),
    )
    await asyncio.wait_for(loop.run(asyncio.Event()), timeout=5)
    assert stopped == [1] and registry.states == []
