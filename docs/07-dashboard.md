# 07 · Watching it: the status API and dashboard

After this chapter you can name every `/api/*` route the dashboard calls and
what each one joins together, explain how the event feed resumes after a
reconnect without replaying history, say which two writes need
`STATUS_DEMO_MODE` and why a custom header is what guards them, and list the
three ways the page keeps showing something useful when a dependency is down.

## What the page shows

`sandbox/status/static/index.html` is one page, plain JavaScript, no build
step: on load it fetches `/api/config`, an initial `/api/fleet`, and then
polls `/api/vms`, `/api/jobs?limit=30`, `/api/sessions?limit=15`, and
`/api/samples?minutes=15` every two seconds through one `refresh()` call. The
header shows the pool, provider, policy, and whether counts came from a
sample or were counted from rows (`renderCounters`); the counters row shows
Total, Idle, Leased, Booting, Draining, Dead, and Pending. The VMs table adds
heartbeat age, provider state, agent version, the current job, and the owner
workflow's id linked to the Temporal UI with its live status next to it. The
events panel prepends each SSE frame, capped at 200 rows in the DOM. Jobs
shows the last 30 job rows with a log button per row; Sessions shows recent
`summary.json` objects — turns, tests passed over total, lint count, cost,
the VM ids, and a link to the owning workflow. A canvas sparkline plots idle,
leased, and pending over the last 15 minutes from `/api/samples`.

Two things on the page only appear when `/api/config` reports
`demo_mode: true`: the `kill`/`stop`/`delete` buttons on each VM row, and the
pool-floor/max edit box in the header. `/api/leases` exists and is exercised
by `tests/status/test_api.py`, but nothing in `index.html` calls it — the
VMs table already carries owner per row (the underlying `vm_view` join
includes `lease_id` too, but the page never renders it), so the page never
needs the separate listing.

## The API

`sandbox.status.api` is a FastAPI app built by `create_app(deps)`, run as its
own process (`Procfile`'s `status` line, `make status` locally).

| Endpoint | Returns |
|---|---|
| `GET /api/config` | pool, provider class name, Temporal UI base and namespace, `demo_mode` |
| `GET /api/fleet` | `joins.fleet_view`: counts, policy, and whether they came from a sample |
| `GET /api/vms` | every registry row joined with provider state, current job, and owner status |
| `GET /api/leases` | leased rows only, with age and owner status |
| `GET /api/jobs?vm_id=&limit=` | recent job rows, filterable by VM |
| `GET /api/jobs/{vm_id}/{job_id}/log` | the tail of `stdout.log` or `stderr.log` from the object store |
| `GET /api/sessions?limit=` | `summary.json` objects under `s3://sandbox-out/`, newest first |
| `GET /api/samples?minutes=` | `fleet_sample` events for the sparkline |
| `GET /api/events` | the SSE stream |
| `POST /api/chaos/{vm_id}/{kill\|stop\|delete}` | demo only |
| `PUT /api/pool/{pool}` | demo only, edits `min_idle`/`max` |

`event_stream` replays events since the client's last-seen id (or the last 30
if none), each as a `control` frame carrying `id: <ts_ulid>`. A browser
`EventSource` remembers that id and sends it back as the `Last-Event-ID`
header on reconnect, so `api_events` passes it through as `last_event_id` and
the generator resumes from just after it — `test_reconnect_with_last_event_id_resumes_without_replaying`
proves a reconnect after event two only replays event three. The first frame
of every stream is a `fleet` snapshot, and one more goes out every other poll
(`poll_seconds`, 1 second by default) so the header counters move without a
separate `/api/fleet` request.

`POST /api/chaos/...` and `PUT /api/pool/...` are the only writes, gated by
`_demo_only`: it 403s unless `STATUS_DEMO_MODE=1` was set when the process
started (`make up` writes it into `.env`), and it 403s again unless the
request carries `X-Sandbox-Demo: 1`. That second check is the CSRF guard: a
plain HTML form (or an image or link) cannot attach a custom header at all,
and a cross-origin script that tries to would trigger a CORS preflight this
app never approves (no CORS middleware is configured), so the browser blocks
the write before it reaches `_demo_only`. Only same-origin script or a
deliberate `curl` can get the header through.

## Joins, not state

`sandbox/status/joins.py` does no I/O; every function takes rows already
fetched and returns the dict the page renders — its own docstring calls the
API module "a thin wiring layer" over it. The registry stays the one place
that decides what is true; a join can only combine or drop fields, never
invent a VM's state. `fleet_view` prefers the reconciler's own count: when a
`fleet_sample` event exists it reports `source: "sample"` and returns those
counts verbatim; only when there is no sample yet (a fresh stack, or the
manager down) does it fall back to counting the rows in `source: "rows"`, per
the module's own reasoning: "where the reconciler has recently sampled the
fleet, its counts are authoritative."

## Degrading, not failing

Three dependencies can be down without a 500. An owner workflow lookup
(`TemporalOwnerStatus`, a Temporal `describe` call) that raises leaves that
row's `owner_status` as `None` — the page shows `?` — and is logged once per
`/api/vms` call rather than once per leased row, so a real outage does not
flood the log. The provider listing (`provider.list()`, the `container` CLI
behind it) is cached for `provider_cache_seconds` and refreshed single-flight
on a miss, so concurrent requests share one call; a failed refresh falls back
to the previous listing, or an empty one if there has never been a successful
call, with unlisted VMs reporting `provider_state: "missing"` rather than
failing the request. `sessions()` caches each parsed `summary.json` by URI
(objects are immutable once written, so a URI only needs one `GET`) and
skips anything that fails to parse as a JSON object with the right shape —
`test_sessions_skips_a_summary_that_is_not_a_json_object` and
`test_sessions_skips_a_summary_deleted_between_list_and_read` both prove a
bad or vanished summary drops out of the list instead of breaking it.

## Read the code

- `sandbox/status/api.py` — `Deps`, `create_app`, `event_stream`,
  `TemporalOwnerStatus`, `_instances`, `_demo_only`.
- `sandbox/status/joins.py` — `fleet_view`, `vm_view`, `lease_view`,
  `session_view`, `sample_series`.
- `sandbox/status/static/index.html` — the page, its polling loop, and the
  SSE handlers.
- `sandbox/status/__init__.py` — what the process serves, in one paragraph.
- `sandbox/cli/chaos.py` — `apply`, `ACTIONS`, shared with
  `make chaos-kill`.
- `sandbox/cli/policy.py` — `merge_policy`, the `min_idle <= max`
  check `PUT /api/pool` reuses.
- `tests/status/test_api.py` — the endpoint and degrade-path tests.
- `tests/status/test_events.py` — the replay and `Last-Event-ID` tests.
- `tests/status/test_joins.py` — the pure view functions, tested with no
  server at all.

## Where this maps in production

`sandbox/status/__init__.py` and `api.py` both say the same thing:
production is "an internal dashboard app over the same API," with the Temporal
client and IAM role of the platform rather than a local dev server and
moto. Nothing about the endpoint shapes or the join logic changes; only the
identity behind `deps.provider` and `deps.owner_status` does, the same
provider-seam split chapter 05 already covers for the manager side.

## Try it

Open `http://localhost:8600` — `make up` already wrote `STATUS_DEMO_MODE=1`
and `make workers` starts the status process alongside the others. Pick an
idle VM's `kill` button and confirm the prompt: the event feed grows a
`chaos` row, then `write_off_stopped`, then a replacement `launch`, and the
VMs table swaps the dead row for a new one within a couple of seconds. Edit
the floor input in the header and click apply; the next fleet frame reflects
the new policy, and a fresh idle VM appears if the floor rose above the
current count.

Next: [08 · Running the demo](08-running-the-demo.md)
