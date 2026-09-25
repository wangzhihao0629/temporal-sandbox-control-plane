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
from datetime import timedelta
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from sandbox.contract.types import DISPOSITIONS
from sandbox.registry.schema import (
    EVENTS_TABLE,
    JOBS_TABLE,
    POOL_ITEM_PREFIX,
    REQUESTS_TABLE,
    STATES,
    VMS_TABLE,
)
from sandbox.timeutil import epoch_in, now, now_iso, to_iso

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
    return to_iso(now() + timedelta(seconds=seconds))


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


def _to_dynamo(value):
    """Coerce a value into what DynamoDB accepts, which excludes floats.

    Callers deal in Python numbers; only this module should have to know that a
    duration or a ratio has to cross as a Decimal. `_clean` is the inverse on
    the way out.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


def _is_condition_failure(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _scan_all(table, **kwargs) -> list[dict]:
    items: list[dict] = []
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _query_all(table, **kwargs) -> list[dict]:
    items: list[dict] = []
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _policy_item(pool, *, min_idle, max, image, cpus, memory) -> dict:
    return {
        "vm_id": f"{POOL_ITEM_PREFIX}{pool}",
        "pool": pool,
        "min_idle": int(min_idle),
        "max": int(max),
        "image": image,
        "cpus": int(cpus),
        "memory": memory,
        "updated_at": now_iso(),
    }


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

    def register_vm(self, vm_id, pool, provider_ref, agent_version, contract_majors, labels=None):
        """Record a booting VM. Never disturbs a live lease.

        A VM agent that re-registers — a restarted agent, a retried boot — must not be
        able to reset a leased row back to an unleased `booting`, or the same VM could
        be handed to a second workflow. So the fresh write is conditional, and an
        existing live row gets an update that leaves `protected` and every lease field
        exactly as the lease holder set them.

        A leased row must not have its `state` rewritten either. `booting` plus live
        lease fields is a row nothing can reach: `claim_idle` will not take it, and
        `AgentRuntime.start` follows the registration with `idle`, which leaves it
        invisible to `find_lease_by_request` and to the orphan scan while still
        holding the lease. So the state change is its own conditional write, and a
        leased row falls back to updating identity fields only.
        """
        ts = now_iso()
        item = {
            "vm_id": vm_id,
            "pool": pool,
            "state": "booting",
            "provider_ref": provider_ref,
            "agent_version": agent_version,
            "contract_majors": list(contract_majors),
            "labels": dict(labels or {}),
            "protected": False,
            "created_at": ts,
            "last_heartbeat_at": ts,
            "last_transition_at": ts,
            "reason": "boot",
        }
        try:
            self.vms.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(vm_id) OR #s IN (:terminated, :dead)",
                ExpressionAttributeNames=_S,
                ExpressionAttributeValues={":terminated": "terminated", ":dead": "dead"},
            )
            return item
        except ClientError as e:
            if not _is_condition_failure(e):
                raise
        identity = (
            "#pool = :pool, provider_ref = :ref, agent_version = :ver, "
            "contract_majors = :majors, labels = :labels, "
            "last_heartbeat_at = :ts, last_transition_at = :ts, reason = :reason"
        )
        values = {
            ":pool": pool,
            ":ref": provider_ref,
            ":ver": agent_version,
            ":majors": list(contract_majors),
            ":labels": dict(labels or {}),
            ":ts": ts,
            ":reason": "re-registered",
        }
        try:
            self.vms.update_item(
                Key={"vm_id": vm_id},
                UpdateExpression=f"SET {identity}, #s = :booting",
                ConditionExpression="attribute_exists(vm_id) AND attribute_not_exists(lease_id)",
                ExpressionAttributeNames={**_S, **_POOL},
                ExpressionAttributeValues={**values, ":booting": "booting"},
            )
            return self.get_vm(vm_id)
        except ClientError as e:
            if not _is_condition_failure(e):
                raise
        self.vms.update_item(
            Key={"vm_id": vm_id},
            UpdateExpression=f"SET {identity}",
            ConditionExpression="attribute_exists(vm_id)",
            ExpressionAttributeNames=dict(_POOL),
            ExpressionAttributeValues=values,
        )
        return self.get_vm(vm_id)

    def get_vm(self, vm_id):
        item = self.vms.get_item(Key={"vm_id": vm_id}).get("Item")
        return _clean(item) if item else None

    def list_vms(self, pool=None):
        rows = _scan_all(self.vms)
        rows = [r for r in rows if not r["vm_id"].startswith(POOL_ITEM_PREFIX)]
        if pool:
            rows = [r for r in rows if r.get("pool") == pool]
        return sorted((_clean(r) for r in rows), key=lambda r: r["created_at"])

    def delete_vm(self, vm_id):
        self.vms.delete_item(Key={"vm_id": vm_id})

    def set_state(self, vm_id, state, *, expect=None, reason="") -> bool:
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}")
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
        candidates = _query_all(
            self.vms,
            IndexName="pool_state_index",
            KeyConditionExpression="#pool = :p AND #s = :idle",
            ExpressionAttributeNames={**_S, **_POOL},
            ExpressionAttributeValues={":p": pool, ":idle": "idle"},
        )
        candidates = [_clean(c) for c in candidates]
        candidates.sort(key=lambda c: c["created_at"])
        wanted = dict(labels or {})
        for row in candidates:
            if contract_major not in row.get("contract_majors", []):
                continue
            if any(row.get("labels", {}).get(k) != v for k, v in wanted.items()):
                continue
            ts = now_iso()
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
                        ":now": ts,
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
        if disposition not in DISPOSITIONS:
            raise ValueError(f"unknown disposition {disposition!r}, expected one of {DISPOSITIONS}")
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

    @staticmethod
    def policy_key(pool: str) -> str:
        return f"{POOL_ITEM_PREFIX}{pool}"

    def get_policy(self, pool):
        item = self.vms.get_item(Key={"vm_id": self.policy_key(pool)}).get("Item")
        return _clean(item) if item else None

    def put_policy(self, pool, *, min_idle, max, image, cpus=2, memory="2048M"):
        item = _policy_item(pool, min_idle=min_idle, max=max, image=image, cpus=cpus, memory=memory)
        self.vms.put_item(Item=item)
        return item

    def ensure_policy(self, pool, *, min_idle, max, image, cpus=2, memory="2048M"):
        """Create the pool item if absent; never overwrite an operator's edits."""
        item = _policy_item(pool, min_idle=min_idle, max=max, image=image, cpus=cpus, memory=memory)
        try:
            self.vms.put_item(Item=item, ConditionExpression="attribute_not_exists(vm_id)")
            return item
        except ClientError as e:
            if _is_condition_failure(e):
                return self.get_policy(pool)
            raise

    def list_pools(self):
        rows = _scan_all(
            self.vms,
            FilterExpression="begins_with(vm_id, :p)",
            ExpressionAttributeValues={":p": POOL_ITEM_PREFIX},
        )
        return sorted((_clean(r) for r in rows), key=lambda r: r["pool"])

    def write_off(self, vm_id, reason) -> bool:
        """Declare a VM gone: terminated, unleased, unprotected, with a TTL.

        Removing the lease fields is deliberate: the old owner's `release`
        conditions on `lease_id` and becomes a no-op, so a dead VM can never be
        flipped back to `recycling` by a late caller.

        Writing a VM off twice is a no-op that returns False: the second write
        would reset `last_transition_at` and the TTL, so a row could be kept out
        of the sweep forever by a caller that keeps rediscovering it, and every
        pass would report a fresh write-off for the same corpse.
        """
        try:
            self.vms.update_item(
                Key={"vm_id": vm_id},
                UpdateExpression=(
                    "SET #s = :s, protected = :false, last_transition_at = :now, "
                    "reason = :reason, #ttl = :ttl REMOVE " + ", ".join(LEASE_FIELDS)
                ),
                ConditionExpression="attribute_exists(vm_id) AND #s <> :terminated",
                ExpressionAttributeNames={**_S, "#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":terminated": "terminated",
                    ":s": "terminated",
                    ":false": False,
                    ":now": now_iso(),
                    ":reason": reason,
                    ":ttl": epoch_in(3600),
                },
            )
            return True
        except ClientError as e:
            if _is_condition_failure(e):
                return False
            raise

    def put_job(self, vm_id, job_id, **fields):
        item = {"vm_id": vm_id, "job_id": job_id, "updated_at": now_iso(), **_to_dynamo(fields)}
        self.jobs.put_item(Item=item)
        return item

    def update_job(self, vm_id, job_id, **fields):
        fields = _to_dynamo(fields)
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
            items = _query_all(
                self.jobs,
                KeyConditionExpression="vm_id = :v",
                ExpressionAttributeValues={":v": vm_id},
            )
        else:
            items = _scan_all(self.jobs)
        rows = sorted(
            (_clean(i) for i in items), key=lambda r: r.get("updated_at", ""), reverse=True
        )
        return rows[:limit]

    def emit(self, type, actor, message, vm_id="", details=None):
        ts = now_iso()
        item = {
            "day": ts[:10],
            "ts_ulid": f"{ts}#{uuid.uuid4().hex[:8]}",
            "type": type,
            "actor": actor,
            "vm_id": vm_id,
            "message": message,
            "details": _to_dynamo(details or {}),
            "ttl": epoch_in(7 * 24 * 3600),
        }
        self.events.put_item(Item=item)
        return item

    def _events_on(self, day, limit):
        return self.events.query(
            KeyConditionExpression="#d = :d",
            ExpressionAttributeNames={"#d": "day"},
            ExpressionAttributeValues={":d": day},
            ScanIndexForward=False,
            Limit=limit,
        ).get("Items", [])

    def recent_events(self, limit=100):
        """The newest events, spilling into yesterday's partition when today is thin.

        The partition key is the UTC date, so at 00:05 today's partition holds
        almost nothing and a dashboard reading only it would show an empty
        system. Each partition comes back newest-first, so today's rows followed
        by yesterday's are already in order.
        """
        rows = self._events_on(now_iso()[:10], limit)
        if len(rows) < limit:
            yesterday = to_iso(now() - timedelta(days=1))[:10]
            rows.extend(self._events_on(yesterday, limit - len(rows)))
        return [_clean(i) for i in rows]

    def latest_fleet_sample(self):
        # Looks back at most 200 events, so a very busy feed can push the last
        # sample out of the window; treat None as "not in the recent window",
        # not "never ran".
        for event in self.recent_events(limit=200):
            if event.get("type") == "fleet_sample":
                return event
        return None

    def record_pending(self, request_id, pool, workflow_id):
        """Record an unfulfilled acquire, keeping the age of the first attempt.

        The client re-asks with the same request_id every few seconds while it
        waits for capacity, so a plain put would reset `created_at` on every
        retry and a queue of starving requests would always look brand new.
        """
        self.requests.update_item(
            Key={"request_id": request_id},
            UpdateExpression=(
                "SET #pool = :pool, #s = :pending, workflow_id = :wf, "
                "created_at = if_not_exists(created_at, :t), #ttl = :ttl"
            ),
            ExpressionAttributeNames={**_S, **_POOL, "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":pool": pool,
                ":pending": "pending",
                ":wf": workflow_id,
                ":t": now_iso(),
                ":ttl": epoch_in(24 * 3600),
            },
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

    def pending_requests(self, pool):
        rows = _scan_all(
            self.requests,
            FilterExpression="#s = :p AND #pool = :pool",
            ExpressionAttributeNames={**_S, **_POOL},
            ExpressionAttributeValues={":p": "pending", ":pool": pool},
        )
        return sorted((_clean(r) for r in rows), key=lambda r: r["created_at"])

    def abandon_request(self, request_id) -> bool:
        try:
            self.requests.update_item(
                Key={"request_id": request_id},
                UpdateExpression="SET #s = :a, abandoned_at = :t",
                ConditionExpression="#s = :p",
                ExpressionAttributeNames=_S,
                ExpressionAttributeValues={":a": "abandoned", ":p": "pending", ":t": now_iso()},
            )
            return True
        except ClientError as e:
            if _is_condition_failure(e):
                return False
            raise

    def pending_count(self, pool) -> int:
        return len(self.pending_requests(pool))
