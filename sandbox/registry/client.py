"""Registry client.

What: every read and write the manager, the VM agent, and the dashboard make.
Why: conditional writes are the whole point. `claim_idle` cannot hand one VM to
two workflows, `release` is idempotent on the lease id, and `set_state` can
insist on the state it expects to leave. Nothing else in the system touches
the tables directly.
Production: identical. The VM agent's IAM role limits it to its own vm_id.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from sandbox.registry.schema import (
    EVENTS_TABLE,
    JOBS_TABLE,
    POOL_ITEM_PREFIX,
    REQUESTS_TABLE,
    VMS_TABLE,
)
from sandbox.timeutil import epoch_in, now_iso, to_iso

LEASE_FIELDS = (
    "lease_id",
    "lease_request_id",
    "owner_workflow_id",
    "owner_run_id",
    "lease_started_at",
    "lease_expires_at",
    "lease_hold_seconds",
)

_S = {"#s": "state"}
# `pool` is a DynamoDB reserved word too, so it needs the same aliasing as `state`.
_POOL = {"#pool": "pool"}


def _iso_in(seconds: int) -> str:
    """ISO timestamp `seconds` from now, for lease expiries."""
    return to_iso(datetime.now(UTC) + timedelta(seconds=seconds))


def _clean(item):
    if isinstance(item, dict):
        return {k: _clean(v) for k, v in item.items()}
    if isinstance(item, list):
        return [_clean(v) for v in item]
    if isinstance(item, set):
        return sorted(_clean(v) for v in item)
    if isinstance(item, Decimal):
        return int(item) if item == item.to_integral_value() else float(item)
    return item


def _is_condition_failure(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class Registry:
    def __init__(self, resource):
        self.vms = resource.Table(VMS_TABLE)
        self.jobs = resource.Table(JOBS_TABLE)
        self.events = resource.Table(EVENTS_TABLE)
        self.requests = resource.Table(REQUESTS_TABLE)

    @staticmethod
    def resource_from_env():
        return boto3.resource(
            "dynamodb",
            endpoint_url=os.environ.get("DYNAMODB_ENDPOINT") or None,
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )

    @classmethod
    def from_env(cls) -> "Registry":
        return cls(cls.resource_from_env())

    # ---- VM rows -----------------------------------------------------------

    def register_vm(self, vm_id, pool, provider_ref, agent_version, contract_majors, labels=None):
        now = now_iso()
        item = {
            "vm_id": vm_id,
            "pool": pool,
            "state": "booting",
            "provider_ref": provider_ref,
            "agent_version": agent_version,
            "contract_majors": list(contract_majors),
            "labels": dict(labels or {}),
            "protected": False,
            "created_at": now,
            "last_heartbeat_at": now,
            "last_transition_at": now,
            "reason": "boot",
        }
        self.vms.put_item(Item=item)
        return item

    def get_vm(self, vm_id):
        item = self.vms.get_item(Key={"vm_id": vm_id}).get("Item")
        return _clean(item) if item else None

    def list_vms(self, pool=None):
        rows = []
        kwargs = {}
        while True:
            page = self.vms.scan(**kwargs)
            rows.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        rows = [r for r in rows if not r["vm_id"].startswith(POOL_ITEM_PREFIX)]
        if pool:
            rows = [r for r in rows if r.get("pool") == pool]
        return sorted((_clean(r) for r in rows), key=lambda r: r["created_at"])

    def delete_vm(self, vm_id):
        self.vms.delete_item(Key={"vm_id": vm_id})

    def set_state(self, vm_id, state, *, expect=None, reason="") -> bool:
        expr = "SET #s = :s, last_transition_at = :t, reason = :r"
        values = {":s": state, ":t": now_iso(), ":r": reason}
        names = dict(_S)
        condition = "attribute_exists(vm_id)"
        if expect is not None:
            condition += " AND #s = :expect"
            values[":expect"] = expect
        if state == "terminated":
            expr += ", #ttl = :ttl"
            names["#ttl"] = "ttl"
            values[":ttl"] = epoch_in(3600)
        try:
            self.vms.update_item(
                Key={"vm_id": vm_id},
                UpdateExpression=expr,
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as e:
            if _is_condition_failure(e):
                return False
            raise

    def heartbeat(self, vm_id):
        """Bump the heartbeat and return the row's state, or None if the row is gone."""
        try:
            out = self.vms.update_item(
                Key={"vm_id": vm_id},
                UpdateExpression="SET last_heartbeat_at = :t",
                ConditionExpression="attribute_exists(vm_id)",
                ExpressionAttributeValues={":t": now_iso()},
                ReturnValues="ALL_NEW",
            )
            return out["Attributes"]["state"]
        except ClientError as e:
            if _is_condition_failure(e):
                return None
            raise

    def claim_idle(
        self,
        pool,
        *,
        lease_id,
        request_id,
        owner_workflow_id,
        owner_run_id,
        hold_seconds,
        contract_major,
        labels,
    ):
        """Atomically move one idle VM to leased. Returns the row or None."""
        candidates = self.vms.query(
            IndexName="pool_state_index",
            KeyConditionExpression="#pool = :p AND #s = :idle",
            ExpressionAttributeNames={**_S, **_POOL},
            ExpressionAttributeValues={":p": pool, ":idle": "idle"},
        ).get("Items", [])
        candidates = [_clean(c) for c in candidates]
        candidates.sort(key=lambda c: c["created_at"])
        wanted = dict(labels or {})
        for row in candidates:
            if contract_major not in row.get("contract_majors", []):
                continue
            if any(row.get("labels", {}).get(k) != v for k, v in wanted.items()):
                continue
            now = now_iso()
            try:
                out = self.vms.update_item(
                    Key={"vm_id": row["vm_id"]},
                    UpdateExpression=(
                        "SET #s = :leased, protected = :true, last_transition_at = :now, "
                        "reason = :reason, lease_id = :lease, lease_request_id = :req, "
                        "owner_workflow_id = :wf, owner_run_id = :run, lease_started_at = :now, "
                        "lease_expires_at = :exp, lease_hold_seconds = :hold"
                    ),
                    ConditionExpression="#s = :idle AND attribute_not_exists(lease_id)",
                    ExpressionAttributeNames=_S,
                    ExpressionAttributeValues={
                        ":leased": "leased",
                        ":idle": "idle",
                        ":true": True,
                        ":now": now,
                        ":reason": "acquired",
                        ":lease": lease_id,
                        ":req": request_id,
                        ":wf": owner_workflow_id,
                        ":run": owner_run_id,
                        ":exp": _iso_in(hold_seconds),
                        ":hold": int(hold_seconds),
                    },
                    ReturnValues="ALL_NEW",
                )
                return _clean(out["Attributes"])
            except ClientError as e:
                if _is_condition_failure(e):
                    continue
                raise
        return None

    def find_lease_by_request(self, request_id):
        items = self.vms.query(
            IndexName="lease_request_index",
            KeyConditionExpression="lease_request_id = :r",
            ExpressionAttributeValues={":r": request_id},
        ).get("Items", [])
        for item in items:
            if item.get("state") == "leased":
                return _clean(item)
        return None

    def release(self, vm_id, lease_id, disposition) -> bool:
        new_state = "recycling" if disposition == "recycle" else "terminating"
        try:
            self.vms.update_item(
                Key={"vm_id": vm_id},
                UpdateExpression=(
                    "SET #s = :s, protected = :false, last_transition_at = :now, reason = :reason "
                    "REMOVE " + ", ".join(LEASE_FIELDS)
                ),
                ConditionExpression="lease_id = :lease",
                ExpressionAttributeNames=_S,
                ExpressionAttributeValues={
                    ":s": new_state,
                    ":false": False,
                    ":now": now_iso(),
                    ":reason": f"released:{disposition}",
                    ":lease": lease_id,
                },
            )
            return True
        except ClientError as e:
            if _is_condition_failure(e):
                return False
            raise

    def touch_lease(self, vm_id) -> None:
        """Extend the lease expiry by the hold recorded at claim time. No-op if not leased."""
        row = self.get_vm(vm_id)
        if not row or "lease_id" not in row:
            return
        hold = int(row.get("lease_hold_seconds", 1800))
        expires = _iso_in(hold)
        try:
            self.vms.update_item(
                Key={"vm_id": vm_id},
                UpdateExpression="SET lease_expires_at = :e, last_heartbeat_at = :t",
                ConditionExpression="lease_id = :lease",
                ExpressionAttributeValues={
                    ":e": expires,
                    ":t": now_iso(),
                    ":lease": row["lease_id"],
                },
            )
        except ClientError as e:
            if not _is_condition_failure(e):
                raise

    # ---- jobs ---------------------------------------------------------------

    def put_job(self, vm_id, job_id, **fields):
        item = {"vm_id": vm_id, "job_id": job_id, "updated_at": now_iso(), **fields}
        self.jobs.put_item(Item=item)
        return item

    def update_job(self, vm_id, job_id, **fields):
        fields["updated_at"] = now_iso()
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":v{i}": v for i, v in enumerate(fields.values())}
        expr = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        self.jobs.update_item(
            Key={"vm_id": vm_id, "job_id": job_id},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def list_jobs(self, vm_id=None, limit=50):
        if vm_id:
            items = self.jobs.query(
                KeyConditionExpression="vm_id = :v", ExpressionAttributeValues={":v": vm_id}
            ).get("Items", [])
        else:
            items = self.jobs.scan().get("Items", [])
        rows = sorted(
            (_clean(i) for i in items), key=lambda r: r.get("updated_at", ""), reverse=True
        )
        return rows[:limit]

    # ---- events -------------------------------------------------------------

    def emit(self, type, actor, message, vm_id="", details=None):
        now = now_iso()
        item = {
            "day": now[:10],
            "ts_ulid": f"{now}#{uuid.uuid4().hex[:8]}",
            "type": type,
            "actor": actor,
            "vm_id": vm_id,
            "message": message,
            "details": details or {},
            "ttl": epoch_in(7 * 24 * 3600),
        }
        self.events.put_item(Item=item)
        return item

    def recent_events(self, limit=100):
        today = now_iso()[:10]
        items = self.events.query(
            KeyConditionExpression="#d = :d",
            ExpressionAttributeNames={"#d": "day"},
            ExpressionAttributeValues={":d": today},
            ScanIndexForward=False,
            Limit=limit,
        ).get("Items", [])
        return [_clean(i) for i in items]

    # ---- acquire requests -----------------------------------------------------

    def record_pending(self, request_id, pool, workflow_id):
        self.requests.put_item(
            Item={
                "request_id": request_id,
                "pool": pool,
                "state": "pending",
                "workflow_id": workflow_id,
                "created_at": now_iso(),
                "ttl": epoch_in(24 * 3600),
            }
        )

    def fulfill_request(self, request_id):
        try:
            self.requests.update_item(
                Key={"request_id": request_id},
                UpdateExpression="SET #s = :f, fulfilled_at = :t",
                ConditionExpression="attribute_exists(request_id)",
                ExpressionAttributeNames=_S,
                ExpressionAttributeValues={":f": "fulfilled", ":t": now_iso()},
            )
        except ClientError as e:
            if not _is_condition_failure(e):
                raise

    def pending_count(self, pool) -> int:
        items = self.requests.scan(
            FilterExpression="#s = :p AND #pool = :pool",
            ExpressionAttributeNames={**_S, **_POOL},
            ExpressionAttributeValues={":p": "pending", ":pool": pool},
        ).get("Items", [])
        return len(items)
