"""The status API.

What: a FastAPI app factory over the registry, the object store, the pool
provider, and a Temporal describe lookup, serving the dashboard page and
`/api/*`; `TemporalOwnerStatus` for the host process; `main()` runs uvicorn.
Why: dependencies come in through `Deps` so the tests hand in moto-backed
fakes and the host process hands in the real clients. Every blocking call
(boto3, the `container` CLI behind `provider.list()`) runs in a thread so a
slow provider never stalls the event loop, and the provider listing is cached
for a couple of seconds because the page polls that often. `/api/jobs` stays
a full scan of the registry (it has no better index), but the page polls it
with `limit=30` and the demo's job count stays in the hundreds, so this is
fine. Read-only, except two demo endpoints that Task 3 adds behind
`STATUS_DEMO_MODE=1`.
Production: an internal dashboard app over the same API, with the Temporal client and
IAM role of the platform.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from sandbox.manager import chaos
from sandbox.manager.policy_cli import merge_policy
from sandbox.manager.providers.apple_container import ProviderError
from sandbox.objectstore import ObjectStore
from sandbox.registry.client import Registry
from sandbox.status import joins
from sandbox.timeutil import now

logger = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
SESSION_PREFIX = "s3://sandbox-out/"
OwnerStatus = Callable[[str, str], Awaitable[str | None]]


@dataclass
class Deps:
    registry: Registry
    store: ObjectStore
    provider: object
    owner_status: OwnerStatus
    temporal_ui: str
    temporal_namespace: str
    pool: str
    demo_mode: bool
    poll_seconds: float = 1.0
    provider_cache_seconds: float = 2.0
    _cache: dict = field(default_factory=dict, repr=False)
    _provider_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class TemporalOwnerStatus:
    """Owner workflow status through a lazily connected Temporal client."""

    def __init__(self, address: str, namespace: str) -> None:
        self.address = address
        self.namespace = namespace
        self._client: Client | None = None
        self._connect_lock = asyncio.Lock()

    async def __call__(self, workflow_id: str, run_id: str) -> str | None:
        if self._client is None:
            async with self._connect_lock:
                if self._client is None:
                    self._client = await Client.connect(self.address, namespace=self.namespace)
        try:
            handle = self._client.get_workflow_handle(workflow_id, run_id=run_id or None)
            desc = await handle.describe(rpc_timeout=timedelta(seconds=2))
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return None
            raise
        return desc.status.name if desc.status is not None else None


def _cache_fresh(cache: dict, provider_cache_seconds: float) -> bool:
    return cache.get("at", 0.0) + provider_cache_seconds > time.monotonic() and "by_ref" in cache


async def _instances(deps: Deps) -> dict[str, dict]:
    """Provider instances by provider_ref, cached for `provider_cache_seconds`.

    A cache miss is single-flight: concurrent callers share one `provider.list()` call
    instead of each kicking off a redundant thread. On failure, log a warning and fall
    back to the previous listing (rows read `provider_state: "missing"` for anything not
    in it, `{}` if there has never been a successful listing) rather than raising and
    500ing `/api/vms` and `/api/leases`.
    """
    cache = deps._cache
    if _cache_fresh(cache, deps.provider_cache_seconds):
        return cache["by_ref"]
    async with deps._provider_lock:
        if _cache_fresh(cache, deps.provider_cache_seconds):
            return cache["by_ref"]
        try:
            instances = await asyncio.to_thread(deps.provider.list)
        except Exception:
            logger.warning("provider.list() failed; showing the previous listing", exc_info=True)
            cache["at"] = time.monotonic()
            return cache.get("by_ref", {})
        by_ref = {}
        for inst in instances:
            item = inst if isinstance(inst, dict) else vars(inst)
            by_ref[item["provider_ref"]] = item
        cache["by_ref"] = by_ref
        cache["at"] = time.monotonic()
        return by_ref


async def _owner_statuses(deps: Deps, rows: list[dict]) -> dict[tuple[str, str], str | None]:
    """Owner workflow status per `(workflow_id, run_id)`, `None` for a leased row on failure.

    A Temporal outage should not 500 `/api/vms` or `/api/leases`; the page just shows `?`
    for the owner status. Logged once per call, not once per row, so an outage does not
    spam the log for every leased VM.
    """
    statuses: dict[tuple[str, str], str | None] = {}
    logged = False
    for row in rows:
        if row.get("state") == "leased" and row.get("owner_workflow_id"):
            key = (row["owner_workflow_id"], row.get("owner_run_id", ""))
            if key not in statuses:
                try:
                    statuses[key] = await deps.owner_status(*key)
                except Exception:
                    statuses[key] = None
                    if not logged:
                        logger.warning(
                            "owner_status lookup failed; showing owner status as unknown",
                            exc_info=True,
                        )
                        logged = True
    return statuses


def _running_job(jobs: list[dict]) -> dict | None:
    for job in jobs:
        if job.get("status") == "running":
            return job
    return None


async def fleet(deps: Deps) -> dict:
    registry = deps.registry
    sample, rows, pending, policy = await asyncio.gather(
        asyncio.to_thread(registry.latest_fleet_sample),
        asyncio.to_thread(registry.list_vms, deps.pool),
        asyncio.to_thread(registry.pending_count, deps.pool),
        asyncio.to_thread(registry.get_policy, deps.pool),
    )
    return joins.fleet_view(sample, rows, pending, policy)


async def vms(deps: Deps) -> list[dict]:
    rows = await asyncio.to_thread(deps.registry.list_vms)
    by_ref, statuses = await asyncio.gather(_instances(deps), _owner_statuses(deps, rows))
    jobs_per_vm = await asyncio.gather(
        *(asyncio.to_thread(deps.registry.list_jobs, row["vm_id"], 10) for row in rows)
    )
    current = now()
    views = []
    for row, jobs in zip(rows, jobs_per_vm, strict=True):
        key = (row.get("owner_workflow_id", ""), row.get("owner_run_id", ""))
        view = joins.vm_view(
            row,
            by_ref.get(row.get("provider_ref", "")),
            _running_job(jobs),
            statuses.get(key),
            current,
        )
        view["owner_link"] = (
            joins.temporal_link(deps.temporal_ui, deps.temporal_namespace, key[0], key[1])
            if key[0]
            else ""
        )
        views.append(view)
    return views


async def leases(deps: Deps) -> list[dict]:
    all_rows = await asyncio.to_thread(deps.registry.list_vms)
    rows = [r for r in all_rows if r.get("state") == "leased"]
    statuses = await _owner_statuses(deps, rows)
    current = now()
    return [
        joins.lease_view(
            row,
            statuses.get((row.get("owner_workflow_id", ""), row.get("owner_run_id", ""))),
            current,
        )
        for row in rows
    ]


async def sessions(deps: Deps, limit: int) -> list[dict]:
    """Session views from `summary.json` objects, parsed once per URI and then cached.

    A summary object is immutable once written, so `deps._cache["summaries"]` (URI ->
    view dict) only needs a GET the first time a URI is seen; later polls LIST again (to
    pick up new sessions and drop ones no longer listed) but skip the GET for URIs
    already cached.
    """

    async def _summary(uri: str) -> dict | None:
        try:
            data = await asyncio.to_thread(deps.store.get_json, uri)
            if not isinstance(data, dict):
                return None
            return joins.session_view(data)
        except (
            json.JSONDecodeError,
            TypeError,
            AttributeError,
            FileNotFoundError,
            ValueError,
            KeyError,
        ):
            return None

    uris = await asyncio.to_thread(deps.store.list, SESSION_PREFIX)
    session_uris = [u for u in uris if u.endswith("/summary.json")]
    cache: dict[str, dict] = deps._cache.setdefault("summaries", {})
    missing = [u for u in session_uris if u not in cache]
    fetched = await asyncio.gather(*(_summary(u) for u in missing))
    for uri, view in zip(missing, fetched, strict=True):
        if view is not None:
            cache[uri] = view
    for uri in list(cache):
        if uri not in session_uris:
            del cache[uri]
    views = []
    for uri in session_uris:
        if uri not in cache:
            continue
        view = dict(cache[uri])
        view["workflow_link"] = joins.temporal_link(
            deps.temporal_ui, deps.temporal_namespace, view["workflow_id"]
        )
        views.append(view)
    views.sort(key=lambda v: v["finished_at"], reverse=True)
    return views[:limit]


def _control_frame(event: dict) -> str:
    ts, _, _ = event["ts_ulid"].partition("#")
    payload = {
        "ts": ts,
        "type": event.get("type", ""),
        "actor": event.get("actor", ""),
        "vm_id": event.get("vm_id", ""),
        "message": event.get("message", ""),
        "details": event.get("details", {}),
    }
    return f"event: control\ndata: {json.dumps(payload)}\n\n"


async def event_stream(deps: Deps, max_events: int | None = None) -> AsyncIterator[str]:
    """Replay the last 30 events oldest first, then poll and push new ones.

    Every second poll also carries a `fleet` frame so the header counters move
    without the page polling `/api/fleet` separately. `max_events` bounds the
    stream for tests; the server streams until the client disconnects.
    """
    sent = 0
    seen: set[str] = set()
    fleet_frame = f"event: fleet\ndata: {json.dumps(await fleet(deps))}\n\n"
    yield fleet_frame
    sent += 1
    if max_events is not None and sent >= max_events:
        return
    recent = await asyncio.to_thread(deps.registry.recent_events, 30)
    for event in reversed(recent):
        seen.add(event["ts_ulid"])
        yield _control_frame(event)
        sent += 1
        if max_events is not None and sent >= max_events:
            return
    tick = 0
    while True:
        await asyncio.sleep(deps.poll_seconds)
        tick += 1
        recent = await asyncio.to_thread(deps.registry.recent_events, 50)
        for event in reversed(recent):
            if event["ts_ulid"] in seen:
                continue
            seen.add(event["ts_ulid"])
            yield _control_frame(event)
            sent += 1
            if max_events is not None and sent >= max_events:
                return
        if tick % 2 == 0:
            yield f"event: fleet\ndata: {json.dumps(await fleet(deps))}\n\n"
            sent += 1
            if max_events is not None and sent >= max_events:
                return
        if len(seen) > 5000:
            seen = set(e["ts_ulid"] for e in recent)


def create_app(deps: Deps) -> FastAPI:
    app = FastAPI(title="sandbox status", docs_url=None, redoc_url=None)
    app.state.deps = deps

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/api/config")
    async def config():
        return {
            "pool": deps.pool,
            "provider": type(deps.provider).__name__,
            "temporal_ui": deps.temporal_ui,
            "temporal_namespace": deps.temporal_namespace,
            "demo_mode": deps.demo_mode,
        }

    @app.get("/api/fleet")
    async def api_fleet():
        return await fleet(deps)

    @app.get("/api/vms")
    async def api_vms():
        return await vms(deps)

    @app.get("/api/leases")
    async def api_leases():
        return await leases(deps)

    @app.get("/api/jobs")
    async def api_jobs(vm_id: str = "", limit: int = Query(50, ge=1, le=500)):
        rows = await asyncio.to_thread(deps.registry.list_jobs, vm_id or None, limit)
        return [joins.job_view(r) for r in rows]

    @app.get("/api/jobs/{vm_id}/{job_id}/log", response_class=PlainTextResponse)
    async def api_job_log(
        vm_id: str,
        job_id: str,
        stream: str = Query("stdout", pattern="^(stdout|stderr)$"),
        tail: int = Query(4096, ge=1, le=1_000_000),
    ):
        rows = await asyncio.to_thread(deps.registry.list_jobs, vm_id, 500)
        row = next((r for r in rows if r.get("job_id") == job_id), None)
        if row is None or not row.get("log_uri"):
            raise HTTPException(404, "no such job, or it has no log")
        try:
            data = await asyncio.to_thread(
                deps.store.get_bytes, f"{row['log_uri'].rstrip('/')}/{stream}.log"
            )
        except FileNotFoundError:
            raise HTTPException(404, f"no {stream} log uploaded yet") from None
        return joins.tail_text(data, tail)

    @app.get("/api/sessions")
    async def api_sessions(limit: int = Query(20, ge=1, le=200)):
        return await sessions(deps, limit)

    @app.get("/api/samples")
    async def api_samples(minutes: int = Query(15, ge=1, le=1440)):
        events = await asyncio.to_thread(deps.registry.recent_events, 200)
        return joins.sample_series(events, minutes, now())

    @app.get("/api/events")
    async def api_events():
        return StreamingResponse(
            event_stream(deps),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _demo_only():
        if not deps.demo_mode:
            raise HTTPException(403, "demo endpoints are disabled; start with STATUS_DEMO_MODE=1")

    @app.post("/api/chaos/{vm_id}/{action}")
    async def api_chaos(vm_id: str, action: str):
        _demo_only()
        if action not in chaos.ACTIONS:
            raise HTTPException(422, f"action must be one of {chaos.ACTIONS}")
        row = await asyncio.to_thread(deps.registry.get_vm, vm_id)
        if row is None:
            raise HTTPException(404, f"no VM {vm_id}")
        try:
            event = await asyncio.to_thread(
                chaos.apply, deps.provider, deps.registry, action, vm_id
            )
        except ProviderError as e:
            raise HTTPException(409, str(e)) from None
        return {"type": event["type"], "message": event["message"], "vm_id": vm_id}

    @app.put("/api/pool/{pool}")
    async def api_pool(pool: str, body: dict = Body(...)):  # noqa: B008 -- fastapi.Body
        _demo_only()
        current = await asyncio.to_thread(deps.registry.get_policy, pool)
        if current is None:
            raise HTTPException(404, f"pool {pool!r} has no policy")
        try:
            merged = merge_policy(current, body.get("min_idle"), body.get("max"), None)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return await asyncio.to_thread(deps.registry.put_policy, pool, **merged)

    return app


def deps_from_env() -> Deps:
    from sandbox.manager.providers.apple_container import AppleContainerProvider

    return Deps(
        registry=Registry.from_env(),
        store=ObjectStore.from_env(),
        provider=AppleContainerProvider(image=os.environ.get("SANDBOX_VM_IMAGE", "sandbox-vm:dev")),
        owner_status=TemporalOwnerStatus(
            os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
            os.environ.get("TEMPORAL_NAMESPACE", "default"),
        ),
        temporal_ui=os.environ.get("TEMPORAL_UI", "http://localhost:8233"),
        temporal_namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        pool=os.environ.get("SANDBOX_POOL", "demo"),
        demo_mode=os.environ.get("STATUS_DEMO_MODE", "") == "1",
    )


def main() -> None:
    import uvicorn

    from sandbox import envfile

    envfile.load()
    port = int(os.environ.get("STATUS_PORT", "8600"))
    uvicorn.run(create_app(deps_from_env()), host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
