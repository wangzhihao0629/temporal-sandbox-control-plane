"""Pure view functions for the dashboard.

What: turn registry rows, provider instances, owner workflow statuses, job
rows, `summary.json` objects, and `fleet_sample` events into the JSON the page
consumes; no I/O anywhere in this module.
Why: the shapes the page depends on are tested here without a server or a
database, and the API module stays a thin wiring layer. Where the reconciler
has recently sampled the fleet, its counts are authoritative; when it has not
(the manager is down, or nothing has run yet), the counts fall back to the
rows so the header is never blank.
Production: identical.
"""

from datetime import datetime, timedelta

from sandbox.timeutil import parse_iso, to_iso

LIVE_STATES = ("booting", "idle", "leased", "recycling", "draining")
DEAD_STATES = ("terminated", "dead")
COUNT_KEYS = (
    "total",
    "idle",
    "leased",
    "booting",
    "recycling",
    "draining",
    "dead",
    "pending",
    "min_idle",
    "max",
)
JOB_KEYS = (
    "vm_id",
    "job_id",
    "status",
    "exit_code",
    "argv_summary",
    "started_at",
    "ended_at",
    "duration_seconds",
    "log_uri",
    "owner_workflow_id",
    "updated_at",
)


def _age(iso: str | None, now: datetime) -> int | None:
    if not iso:
        return None
    return max(0, int((now - parse_iso(iso)).total_seconds()))


def heartbeat_age(row: dict, now: datetime) -> int:
    return _age(row.get("last_heartbeat_at"), now) or 0


def temporal_link(ui: str, namespace: str, workflow_id: str, run_id: str = "") -> str:
    link = f"{ui.rstrip('/')}/namespaces/{namespace}/workflows/{workflow_id}"
    return f"{link}/{run_id}" if run_id else link


def vm_view(
    row: dict,
    instance: dict | None,
    current_job: dict | None,
    owner_status: str | None,
    now: datetime,
) -> dict:
    return {
        "vm_id": row["vm_id"],
        "pool": row.get("pool", ""),
        "state": row.get("state", ""),
        "reason": row.get("reason", ""),
        "heartbeat_age_seconds": heartbeat_age(row, now),
        "agent_version": row.get("agent_version", ""),
        "protected": bool(row.get("protected", False)),
        "created_at": row.get("created_at", ""),
        "provider_state": instance["state"] if instance else "missing",
        "address": (instance or {}).get("address", "") or "",
        "lease_id": row.get("lease_id", ""),
        "owner_workflow_id": row.get("owner_workflow_id", ""),
        "owner_run_id": row.get("owner_run_id", ""),
        "owner_status": owner_status,
        "lease_age_seconds": _age(row.get("lease_started_at"), now),
        "lease_expires_at": row.get("lease_expires_at", ""),
        "current_job": (
            {
                "job_id": current_job["job_id"],
                "argv_summary": current_job.get("argv_summary", ""),
                "started_at": current_job.get("started_at", ""),
            }
            if current_job
            else None
        ),
    }


def lease_view(row: dict, owner_status: str | None, now: datetime) -> dict:
    return {
        "vm_id": row["vm_id"],
        "lease_id": row.get("lease_id", ""),
        "request_id": row.get("lease_request_id", ""),
        "owner_workflow_id": row.get("owner_workflow_id", ""),
        "owner_run_id": row.get("owner_run_id", ""),
        "owner_status": owner_status,
        "age_seconds": _age(row.get("lease_started_at"), now),
        "expires_at": row.get("lease_expires_at", ""),
    }


def fleet_view(sample: dict | None, rows: list[dict], pending: int, policy: dict | None) -> dict:
    policy_view = (
        {
            "pool": policy["pool"],
            "min_idle": int(policy["min_idle"]),
            "max": int(policy["max"]),
            "image": policy.get("image", ""),
        }
        if policy
        else None
    )
    if sample:
        details = sample.get("details", {})
        counts = {key: int(details.get(key, 0)) for key in COUNT_KEYS}
        return {
            "source": "sample",
            "sample_at": sample["ts_ulid"].split("#", 1)[0],
            "counts": counts,
            "policy": policy_view,
        }
    by_state: dict[str, int] = {}
    for row in rows:
        by_state[row.get("state", "")] = by_state.get(row.get("state", ""), 0) + 1
    counts = {
        "total": sum(by_state.get(s, 0) for s in LIVE_STATES),
        "idle": by_state.get("idle", 0),
        "leased": by_state.get("leased", 0),
        "booting": by_state.get("booting", 0),
        "recycling": by_state.get("recycling", 0),
        "draining": by_state.get("draining", 0),
        "dead": sum(by_state.get(s, 0) for s in DEAD_STATES),
        "pending": int(pending),
        "min_idle": policy_view["min_idle"] if policy_view else 0,
        "max": policy_view["max"] if policy_view else 0,
    }
    return {"source": "rows", "sample_at": None, "counts": counts, "policy": policy_view}


def job_view(row: dict) -> dict:
    view = {key: row.get(key, "") for key in JOB_KEYS}
    view["exit_code"] = row.get("exit_code")
    view["duration_seconds"] = row.get("duration_seconds")
    return view


def session_view(summary: dict) -> dict:
    session_id = summary.get("session_id", "")
    return {
        "session_id": session_id,
        "workflow_id": summary.get("workflow_id") or f"session-{session_id}",
        "scenario": summary.get("scenario", ""),
        "prompt": summary.get("prompt", ""),
        "turns": int(summary.get("turns", 0)),
        "tests_passed": bool(summary.get("tests_passed", False)),
        "tests_failed": int(summary.get("tests_failed", 0)),
        "tests_total": int(summary.get("tests_total", 0)),
        "lint_count": int(summary.get("lint_count", 0)),
        "fake_cost_usd": float(summary.get("fake_cost_usd", 0.0)),
        "patch_uri": summary.get("patch_uri", ""),
        "vm_ids": list(summary.get("vm_ids", [])),
        "attempts": int(summary.get("attempts", 0)),
        "finished_at": summary.get("finished_at", ""),
    }


def sample_series(events: list[dict], minutes: int, now: datetime) -> list[dict]:
    cutoff = to_iso(now - timedelta(minutes=minutes))
    points = []
    for event in events:
        if event.get("type") != "fleet_sample":
            continue
        ts = event["ts_ulid"].split("#", 1)[0]
        if ts < cutoff:
            continue
        d = event.get("details", {})
        points.append(
            {
                "ts": ts,
                "idle": int(d.get("idle", 0)),
                "leased": int(d.get("leased", 0)),
                "pending": int(d.get("pending", 0)),
                "total": int(d.get("total", 0)),
            }
        )
    points.sort(key=lambda p: p["ts"])
    return points


def tail_text(data: bytes, tail: int) -> str:
    return data[-tail:].decode("utf-8", errors="replace") if tail > 0 else ""
