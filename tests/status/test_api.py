"""The read endpoints join registry, provider, Temporal, and object-store data into JSON."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from sandbox.manager.providers.apple_container import ProviderError
from sandbox.status.api import Deps, create_app
from tests.status.conftest import (
    FlakyListingProvider,
    ListingProvider,
    SlowListingProvider,
    seed_vm,
)


@pytest.fixture
def provider():
    return ListingProvider(
        [
            {
                "provider_ref": "sbx-a",
                "vm_id": "sbx-a",
                "state": "running",
                "created_at": "t",
                "address": "192.168.64.5",
                "gateway": "192.168.64.1",
            }
        ]
    )


@pytest.fixture
def client(backend, provider, owner_statuses):
    registry, store = backend
    deps = Deps(
        registry=registry,
        store=store,
        provider=provider,
        owner_status=owner_statuses,
        temporal_ui="http://localhost:8233",
        temporal_namespace="default",
        pool="demo",
        demo_mode=False,
        provider_cache_seconds=0.0,
    )
    with TestClient(create_app(deps)) as c:
        yield c


def test_index_serves_the_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "Sandbox Fleet" in r.text


def test_config_reports_links_and_mode(client):
    cfg = client.get("/api/config").json()
    assert cfg == {
        "pool": "demo",
        "provider": "ListingProvider",
        "temporal_ui": "http://localhost:8233",
        "temporal_namespace": "default",
        "demo_mode": False,
    }


def test_fleet_falls_back_to_rows_then_prefers_a_sample(client, backend):
    registry, _ = backend
    registry.ensure_policy("demo", min_idle=2, max=5, image="img")
    # sbx-b is seeded (and leased) before sbx-a exists: claim_idle takes the
    # oldest idle VM in the pool, so leasing sbx-b after sbx-a is already idle
    # would hand the lease to sbx-a instead.
    seed_vm(registry, "sbx-b", owner_workflow_id="session-x")
    seed_vm(registry, "sbx-a")
    fleet = client.get("/api/fleet").json()
    assert fleet["source"] == "rows" and fleet["counts"]["idle"] == 1
    assert fleet["counts"]["leased"] == 1 and fleet["policy"]["max"] == 5
    registry.emit(
        "fleet_sample",
        "reconciler",
        "sample",
        details={
            "total": 9,
            "idle": 9,
            "leased": 0,
            "booting": 0,
            "recycling": 0,
            "draining": 0,
            "dead": 0,
            "pending": 0,
            "min_idle": 2,
            "max": 5,
        },
    )
    fleet = client.get("/api/fleet").json()
    assert fleet["source"] == "sample" and fleet["counts"]["total"] == 9 and fleet["sample_at"]


def test_vms_join_provider_job_and_owner(client, backend, owner_statuses):
    registry, _ = backend
    seed_vm(registry, "sbx-a", owner_workflow_id="session-x", owner_run_id="r1")
    seed_vm(registry, "sbx-b")
    registry.put_job(
        "sbx-a",
        "s-turn-t1-a1",
        status="running",
        argv_summary="runner turn",
        owner_workflow_id="session-x",
        started_at="t",
        log_uri="",
    )
    registry.put_job("sbx-a", "old", status="exited", argv_summary="echo", owner_workflow_id="w")
    owner_statuses.table[("session-x", "r1")] = "RUNNING"
    vms = {v["vm_id"]: v for v in client.get("/api/vms").json()}
    a, b = vms["sbx-a"], vms["sbx-b"]
    assert a["state"] == "leased" and a["provider_state"] == "running"
    assert a["address"] == "192.168.64.5" and a["owner_status"] == "RUNNING"
    assert a["current_job"]["job_id"] == "s-turn-t1-a1"
    assert a["owner_link"] == "http://localhost:8233/namespaces/default/workflows/session-x/r1"
    assert b["provider_state"] == "missing" and b["owner_status"] is None
    assert b["current_job"] is None and b["owner_link"] == ""


def test_vms_caches_the_provider_listing(backend, provider, owner_statuses):
    registry, store = backend
    seed_vm(registry, "sbx-a")
    deps = Deps(
        registry,
        store,
        provider,
        owner_statuses,
        "http://ui",
        "default",
        "demo",
        False,
        provider_cache_seconds=60.0,
    )
    with TestClient(create_app(deps)) as c:
        c.get("/api/vms")
        c.get("/api/vms")
    assert provider.list_calls == 1


async def _failing_owner_status(workflow_id: str, run_id: str) -> str | None:
    raise RuntimeError("temporal down")


def test_vms_and_leases_degrade_when_the_owner_lookup_fails(backend, provider):
    registry, store = backend
    seed_vm(registry, "sbx-a", owner_workflow_id="session-x", owner_run_id="r1")
    deps = Deps(
        registry,
        store,
        provider,
        _failing_owner_status,
        "http://ui",
        "default",
        "demo",
        False,
        provider_cache_seconds=0.0,
    )
    with TestClient(create_app(deps)) as c:
        vms_resp = c.get("/api/vms")
        leases_resp = c.get("/api/leases")
    assert vms_resp.status_code == 200 and leases_resp.status_code == 200
    a = next(v for v in vms_resp.json() if v["vm_id"] == "sbx-a")
    assert a["state"] == "leased" and a["owner_status"] is None
    assert leases_resp.json()[0]["owner_status"] is None


def test_instances_falls_back_to_the_stale_listing_on_a_later_failure(backend, owner_statuses):
    registry, store = backend
    seed_vm(registry, "sbx-a")
    flaky_provider = FlakyListingProvider(
        [{"provider_ref": "sbx-a", "vm_id": "sbx-a", "state": "running", "created_at": "t"}],
        fail_on=frozenset({2}),
    )
    deps = Deps(
        registry,
        store,
        flaky_provider,
        owner_statuses,
        "http://ui",
        "default",
        "demo",
        False,
        provider_cache_seconds=0.0,
    )
    with TestClient(create_app(deps)) as c:
        first = c.get("/api/vms")
        second = c.get("/api/vms")
    assert first.status_code == 200 and second.status_code == 200
    assert next(v for v in first.json() if v["vm_id"] == "sbx-a")["provider_state"] == "running"
    assert next(v for v in second.json() if v["vm_id"] == "sbx-a")["provider_state"] == "running"
    assert flaky_provider.list_calls == 2


def test_instances_returns_missing_when_the_first_listing_fails(backend, owner_statuses):
    registry, store = backend
    seed_vm(registry, "sbx-a")
    flaky_provider = FlakyListingProvider([], fail_on=frozenset({1}))
    deps = Deps(
        registry,
        store,
        flaky_provider,
        owner_statuses,
        "http://ui",
        "default",
        "demo",
        False,
        provider_cache_seconds=0.0,
    )
    with TestClient(create_app(deps)) as c:
        r = c.get("/api/vms")
    assert r.status_code == 200
    assert next(v for v in r.json() if v["vm_id"] == "sbx-a")["provider_state"] == "missing"


async def test_instances_is_single_flight_on_a_cache_miss(backend, owner_statuses):
    registry, store = backend
    seed_vm(registry, "sbx-a")
    slow_provider = SlowListingProvider(
        [{"provider_ref": "sbx-a", "vm_id": "sbx-a", "state": "running", "created_at": "t"}],
        delay=0.2,
    )
    deps = Deps(
        registry,
        store,
        slow_provider,
        owner_statuses,
        "http://ui",
        "default",
        "demo",
        False,
        provider_cache_seconds=60.0,
    )
    app = create_app(deps)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        r1, r2 = await asyncio.gather(ac.get("/api/vms"), ac.get("/api/vms"))
    assert r1.status_code == 200 and r2.status_code == 200
    assert slow_provider.list_calls == 1


def test_leases_lists_only_leased_rows(client, backend, owner_statuses):
    registry, _ = backend
    # sbx-b is seeded (and leased) before sbx-a exists, for the same reason as
    # in test_fleet_falls_back_to_rows_then_prefers_a_sample above.
    seed_vm(registry, "sbx-b", owner_workflow_id="session-y", owner_run_id="r2")
    seed_vm(registry, "sbx-a")
    owner_statuses.table[("session-y", "r2")] = "TERMINATED"
    leases = client.get("/api/leases").json()
    assert [lease["vm_id"] for lease in leases] == ["sbx-b"]
    assert leases[0]["owner_status"] == "TERMINATED" and leases[0]["age_seconds"] >= 0


def test_jobs_filter_and_limit(client, backend):
    registry, _ = backend
    for i in range(3):
        registry.put_job("sbx-a", f"j{i}", status="exited", argv_summary=f"cmd {i}")
    registry.put_job("sbx-b", "k0", status="running", argv_summary="other")
    assert len(client.get("/api/jobs").json()) == 4
    only_a = client.get("/api/jobs", params={"vm_id": "sbx-a", "limit": 2}).json()
    assert len(only_a) == 2 and all(j["vm_id"] == "sbx-a" for j in only_a)
    assert "argv_summary" in only_a[0] and "exit_code" in only_a[0]


def test_job_log_tails_the_stream_from_the_object_store(client, backend):
    registry, store = backend
    registry.put_job("sbx-a", "j1", status="exited", log_uri="s3://sandbox-jobs/s/j1")
    store.put_bytes("s3://sandbox-jobs/s/j1/stdout.log", b"[tool] Read a\n[tool] Edit b\n")
    r = client.get("/api/jobs/sbx-a/j1/log", params={"tail": 14})
    assert r.status_code == 200 and r.text == "[tool] Edit b\n"
    assert client.get("/api/jobs/sbx-a/j1/log", params={"stream": "stderr"}).status_code == 404
    assert client.get("/api/jobs/sbx-a/nope/log").status_code == 404
    assert client.get("/api/jobs/sbx-a/j1/log", params={"stream": "x"}).status_code == 422


def test_sessions_newest_first_with_limit(client, backend):
    _, store = backend
    for i, ts in enumerate(
        ["2026-09-10T01:00:00.000Z", "2026-09-10T03:00:00.000Z", "2026-09-10T02:00:00.000Z"]
    ):
        store.put_json(
            f"s3://sandbox-out/s{i}/summary.json",
            {
                "session_id": f"s{i}",
                "turns": i,
                "tests_passed": True,
                "finished_at": ts,
                "workflow_id": f"session-s{i}",
            },
        )
    store.put_bytes("s3://sandbox-out/s1/session.patch", b"not a summary")
    sessions = client.get("/api/sessions").json()
    assert [s["session_id"] for s in sessions] == ["s1", "s2", "s0"]
    assert sessions[0]["workflow_link"].endswith("/workflows/session-s1")
    assert len(client.get("/api/sessions", params={"limit": 2}).json()) == 2


def test_samples_window(client, backend):
    registry, _ = backend
    registry.emit(
        "fleet_sample",
        "reconciler",
        "s",
        details={"idle": 2, "leased": 0, "pending": 0, "total": 2},
    )
    registry.emit("launch", "reconciler", "not a sample")
    points = client.get("/api/samples", params={"minutes": 15}).json()
    assert len(points) == 1 and points[0]["idle"] == 2 and "ts" in points[0]


def test_sessions_skips_a_summary_that_is_not_a_json_object(client, backend):
    _, store = backend
    store.put_json(
        "s3://sandbox-out/good/summary.json",
        {
            "session_id": "good",
            "turns": 1,
            "tests_passed": True,
            "finished_at": "2026-09-10T02:00:00.000Z",
            "workflow_id": "session-good",
        },
    )
    store.put_bytes("s3://sandbox-out/bad/summary.json", b"[1, 2]")
    store.put_bytes("s3://sandbox-out/worse/summary.json", b"{not json")
    sessions = client.get("/api/sessions").json()
    assert [s["session_id"] for s in sessions] == ["good"]


def test_sessions_skips_a_summary_deleted_between_list_and_read(client, backend, monkeypatch):
    _, store = backend
    for i, sid in enumerate(["s0", "s1"]):
        store.put_json(
            f"s3://sandbox-out/{sid}/summary.json",
            {
                "session_id": sid,
                "turns": i,
                "tests_passed": True,
                "finished_at": f"2026-09-10T0{i}:00:00.000Z",
                "workflow_id": f"session-{sid}",
            },
        )
    store.put_json(
        "s3://sandbox-out/gone/summary.json",
        {
            "session_id": "gone",
            "turns": 0,
            "tests_passed": True,
            "finished_at": "2026-09-10T05:00:00.000Z",
            "workflow_id": "session-gone",
        },
    )
    deps = client.app.state.deps
    real_get_json = deps.store.get_json

    def flaky_get_json(uri):
        if uri == "s3://sandbox-out/gone/summary.json":
            raise FileNotFoundError(uri)
        return real_get_json(uri)

    monkeypatch.setattr(deps.store, "get_json", flaky_get_json)
    r = client.get("/api/sessions")
    assert r.status_code == 200
    assert {s["session_id"] for s in r.json()} == {"s0", "s1"}


def test_sessions_only_reads_a_summary_once_across_polls(client, backend, monkeypatch):
    _, store = backend
    for sid, ts in [("s0", "2026-09-10T01:00:00.000Z"), ("s1", "2026-09-10T02:00:00.000Z")]:
        store.put_json(
            f"s3://sandbox-out/{sid}/summary.json",
            {
                "session_id": sid,
                "turns": 1,
                "tests_passed": True,
                "finished_at": ts,
                "workflow_id": f"session-{sid}",
            },
        )
    deps = client.app.state.deps
    calls: list[str] = []
    real_get_json = deps.store.get_json

    def counting_get_json(uri):
        calls.append(uri)
        return real_get_json(uri)

    monkeypatch.setattr(deps.store, "get_json", counting_get_json)
    first = client.get("/api/sessions").json()
    second = client.get("/api/sessions").json()
    assert [s["session_id"] for s in first] == ["s1", "s0"]
    assert [s["session_id"] for s in second] == ["s1", "s0"]
    assert calls.count("s3://sandbox-out/s0/summary.json") == 1
    assert calls.count("s3://sandbox-out/s1/summary.json") == 1


def test_sessions_skips_a_summary_with_a_non_numeric_turns(client, backend):
    _, store = backend
    store.put_json(
        "s3://sandbox-out/good/summary.json",
        {
            "session_id": "good",
            "turns": 1,
            "tests_passed": True,
            "finished_at": "2026-09-10T02:00:00.000Z",
            "workflow_id": "session-good",
        },
    )
    store.put_json(
        "s3://sandbox-out/bad/summary.json",
        {
            "session_id": "bad",
            "turns": "two",
            "tests_passed": True,
            "finished_at": "2026-09-10T03:00:00.000Z",
            "workflow_id": "session-bad",
        },
    )
    sessions = client.get("/api/sessions").json()
    assert [s["session_id"] for s in sessions] == ["good"]


DEMO_HEADERS = {"X-Sandbox-Demo": "1"}


def test_demo_endpoints_are_refused_unless_enabled(client):
    assert client.post("/api/chaos/sbx-a/kill", headers=DEMO_HEADERS).status_code == 403
    assert client.put("/api/pool/demo", json={"max": 3}, headers=DEMO_HEADERS).status_code == 403


@pytest.fixture
def demo_client(backend, provider, owner_statuses):
    registry, store = backend
    deps = Deps(
        registry,
        store,
        provider,
        owner_statuses,
        "http://ui",
        "default",
        "demo",
        True,
        provider_cache_seconds=0.0,
    )
    with TestClient(create_app(deps)) as c:
        yield c


def test_demo_writes_need_the_custom_header_even_when_enabled(demo_client, backend):
    registry, _ = backend
    seed_vm(registry, "sbx-a")
    r = demo_client.post("/api/chaos/sbx-a/kill")
    assert r.status_code == 403 and "X-Sandbox-Demo" in r.json()["detail"]
    r = demo_client.put("/api/pool/demo", json={"max": 3})
    assert r.status_code == 403 and "X-Sandbox-Demo" in r.json()["detail"]


def test_chaos_calls_the_provider_and_records_an_event(demo_client, backend, provider):
    registry, _ = backend
    seed_vm(registry, "sbx-a")
    r = demo_client.post("/api/chaos/sbx-a/kill", headers=DEMO_HEADERS)
    assert r.status_code == 200 and r.json()["type"] == "chaos"
    assert provider.killed == ["sbx-a"]
    r = demo_client.post("/api/chaos/sbx-a/delete", headers=DEMO_HEADERS)
    assert r.status_code == 200 and provider.terminated == ["sbx-a"]
    assert demo_client.post("/api/chaos/sbx-a/explode", headers=DEMO_HEADERS).status_code == 422
    assert demo_client.post("/api/chaos/sbx-zzz/kill", headers=DEMO_HEADERS).status_code == 404
    events = registry.recent_events(10)
    assert [e["type"] for e in events[:2]] == ["chaos", "chaos"] and events[0]["actor"] == "chaos"


def test_chaos_returns_409_when_the_provider_says_the_target_is_gone(
    demo_client, backend, provider, monkeypatch
):
    registry, _ = backend
    seed_vm(registry, "sbx-a")

    def exploding_kill(vm_id):
        raise ProviderError("no such container")

    monkeypatch.setattr(provider, "kill", exploding_kill)
    r = demo_client.post("/api/chaos/sbx-a/kill", headers=DEMO_HEADERS)
    assert r.status_code == 409 and "no such container" in r.json()["detail"]
    assert all(e["type"] != "chaos" for e in registry.recent_events(5))


def test_pool_edit_merges_and_validates(demo_client, backend):
    registry, _ = backend
    assert demo_client.put("/api/pool/demo", json={"max": 3}, headers=DEMO_HEADERS).status_code == 404
    registry.ensure_policy("demo", min_idle=2, max=5, image="img")
    r = demo_client.put("/api/pool/demo", json={"max": 3}, headers=DEMO_HEADERS)
    assert r.status_code == 200 and r.json()["max"] == 3 and r.json()["min_idle"] == 2
    assert (
        demo_client.put("/api/pool/demo", json={"min_idle": 9}, headers=DEMO_HEADERS).status_code
        == 400
    )
    assert registry.get_policy("demo")["max"] == 3
