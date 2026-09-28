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

import re
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


_RUNNER_PREFIX = re.compile(r"^/bin/sh \S*/bin/runner\b")
_ENVELOPE_ARG = re.compile(r" --envelope-uri \S+")


def command_view(argv_summary: str) -> str:
    """The command as a reader wants it: `runner test --workspace ...`, not the
    content-addressed artifact path, and without the envelope URI every step has."""
    return _ENVELOPE_ARG.sub("", _RUNNER_PREFIX.sub("runner", argv_summary or ""))


def event_view(event: dict) -> dict:
    return {
        "ts": event["ts_ulid"].split("#", 1)[0],
        "type": event.get("type", ""),
        "actor": event.get("actor", ""),
        "vm_id": event.get("vm_id", ""),
        "message": event.get("message", ""),
        "details": event.get("details", {}),
    }


def job_view(row: dict) -> dict:
    view = {key: row.get(key, "") for key in JOB_KEYS}
    view["command"] = command_view(row.get("argv_summary", ""))
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


_JOB_ID = re.compile(r"-t(\d+)-a(\d+)$")
_RUNNER_STEP = re.compile(r"^runner (\S+)")
ENVELOPE_URI = re.compile(r"--envelope-uri (\S+)")


def step_view(row: dict) -> dict:
    """One job as a step of its run: what ran, which turn and attempt, and where."""
    command = command_view(row.get("argv_summary", ""))
    runner = _RUNNER_STEP.match(command)
    ids = _JOB_ID.search(row.get("job_id", ""))
    return {
        "job_id": row.get("job_id", ""),
        "vm_id": row.get("vm_id", ""),
        "step": runner.group(1) if runner else (command.split(" ", 1)[0] or "?"),
        "turn": int(ids.group(1)) if ids else None,
        "attempt": int(ids.group(2)) if ids else None,
        "command": command,
        "argv_summary": row.get("argv_summary", ""),
        "status": row.get("status", ""),
        "exit_code": row.get("exit_code"),
        "started_at": row.get("started_at", ""),
        "duration_seconds": row.get("duration_seconds"),
        "log_uri": row.get("log_uri", ""),
        "result": None,
    }


def step_result(step: str, envelope: dict) -> dict:
    """A step's envelope as one line and a level (ok, warn, bad) for the page.

    A failing test is `bad` even though its job exited 0: failures are data the
    runner reports, not errors, so the exit code alone would show it green."""
    if not envelope.get("ok", False):
        return {"text": envelope.get("error") or "broken", "level": "bad"}
    e = envelope
    if step == "test":
        text = f"{e.get('passed', 0)}/{e.get('total', 0)} passed"
        failures = e.get("failures") or []
        if failures:
            return {"text": f"{text} — {failures[0].get('test', '?')}", "level": "bad"}
        return {"text": text, "level": "ok"}
    if step == "lint":
        findings = e.get("findings") or []
        if not findings:
            return {"text": "no findings", "level": "ok"}
        f = findings[0]
        return {
            "text": f"{e.get('count', len(findings))} finding(s) — {f.get('code')} "
            f"{f.get('file')}:{f.get('line')}",
            "level": "warn",
        }
    if step == "run":
        first = (e.get("stdout") or "").splitlines()[:1]
        text = f"→ {first[0]}" if first else "(no output)"
        return {"text": text, "level": "ok" if e.get("exit_code", 0) == 0 else "bad"}
    texts = {
        "clone": lambda: f"from {e.get('source', '?')} at {str(e.get('head', ''))[:7]}",
        "turn": lambda: f"{e.get('summary', '')} ({e.get('files_changed', 0)} files)",
        "export": lambda: f"{e.get('commits', 0)} commit(s), {e.get('bytes', 0)} B patch",
        "fetch": lambda: f"at {str(e.get('head', ''))[:7]}",
        "edit": lambda: "already applied"
        if e.get("already_applied")
        else f"{e.get('file', '')} +{e.get('added', 0)} -{e.get('removed', 0)}",
        "build": lambda: f"{e.get('go_version', '')} · {e.get('bytes', 0) // 1024} KB "
        f"in {e.get('seconds', 0)}s",
    }
    return {"text": texts[step]() if step in texts else "ok", "level": "ok"}


def run_kind(workflow_id: str) -> str:
    return workflow_id.split("-", 1)[0] if "-" in workflow_id else "run"


def run_views(steps_by_workflow: dict[str, list[dict]], link) -> list[dict]:
    """Jobs grouped into the runs that asked for them, newest run first."""
    runs = []
    for workflow_id, steps in steps_by_workflow.items():
        steps = sorted(steps, key=lambda s: s["started_at"])
        running = any(s["status"] == "running" for s in steps)
        levels = [s["result"]["level"] for s in steps if s["result"]]
        vms: list[str] = []
        for s in steps:
            if s["vm_id"] not in vms:
                vms.append(s["vm_id"])
        runs.append(
            {
                "workflow_id": workflow_id,
                "workflow_link": link(workflow_id),
                "kind": run_kind(workflow_id),
                "status": "running" if running else "done",
                "level": "bad" if "bad" in levels else "warn" if "warn" in levels else "ok",
                "started_at": steps[0]["started_at"],
                "last_step": steps[-1],
                "vm_ids": vms,
                "steps": steps,
            }
        )
    runs.sort(key=lambda r: r["last_step"]["started_at"], reverse=True)
    return runs
