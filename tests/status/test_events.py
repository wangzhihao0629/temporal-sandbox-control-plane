"""The SSE generator replays recent events, pushes new ones once, and adds fleet snapshots."""

import json
import time

import pytest

from sandbox.status.api import Deps, event_stream
from tests.status.conftest import ListingProvider


def _parse(frame: str) -> tuple[str, dict]:
    lines = frame.strip().splitlines()
    kind = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
    raw = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
    return kind, json.loads(raw)


@pytest.fixture
def deps(backend, owner_statuses):
    registry, store = backend
    return Deps(
        registry,
        store,
        ListingProvider(),
        owner_statuses,
        "http://ui",
        "default",
        "demo",
        False,
        poll_seconds=0.01,
        provider_cache_seconds=0.0,
    )


async def test_stream_replays_then_pushes_new_events_once(deps):
    deps.registry.emit("boot", "vm-agent", "first", vm_id="sbx-a")
    # ts_ulid keys are a millisecond timestamp plus a random suffix, so emits in the
    # same millisecond have no defined order; space them out so order assertions hold.
    time.sleep(0.002)
    deps.registry.emit("launch", "reconciler", "second", vm_id="sbx-b")
    frames = []
    async for frame in event_stream(deps, max_events=4):
        frames.append(_parse(frame))
        if len(frames) == 2:
            time.sleep(0.002)
            deps.registry.emit("release", "manager", "third", vm_id="sbx-a")
    kinds = [k for k, _ in frames]
    controls = [d for k, d in frames if k == "control"]
    assert kinds.count("control") == 3 and "fleet" in kinds
    messages = [c["message"] for c in controls]
    assert messages == ["first", "second", "third"], "oldest first, no repeats"
    assert controls[0]["vm_id"] == "sbx-a" and controls[0]["type"] == "boot"
    assert all("ts" in c and "actor" in c for c in controls)


async def test_fleet_frames_carry_counts(deps):
    async for frame in event_stream(deps, max_events=1):
        kind, data = _parse(frame)
    assert kind == "fleet" and set(data["counts"]) >= {"idle", "leased", "pending"}


async def test_reconnect_with_last_event_id_resumes_without_replaying(deps):
    deps.registry.emit("boot", "vm-agent", "first", vm_id="sbx-a")
    time.sleep(0.002)
    second = deps.registry.emit("launch", "reconciler", "second", vm_id="sbx-b")
    time.sleep(0.002)
    deps.registry.emit("release", "manager", "third", vm_id="sbx-a")
    frames = []
    async for frame in event_stream(deps, max_events=2, last_event_id=second["ts_ulid"]):
        frames.append(_parse(frame))
    kinds = [k for k, _ in frames]
    assert kinds == ["fleet", "control"]
    assert frames[1][1]["message"] == "third"
