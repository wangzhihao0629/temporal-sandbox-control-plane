"""Acquire and release.

What: the two manager activities a workflow's client calls.
Why: acquire is idempotent on the caller's request_id, so a workflow that
crashes between claiming and recording finds its lease again. It never waits;
no capacity is a non-retryable NoCapacity that the client turns into a sleep
loop, and the pending request is recorded so it counts toward scale-out.
Release is idempotent on the lease id and only flips the row; the VM finishes
the recycle itself.
Production: identical. These run as sync activities in a thread pool because
boto3 is blocking.
"""

import uuid

from temporalio import activity

from sandbox.contract import names
from sandbox.contract.errors import Incompatible, NoCapacity
from sandbox.contract.types import ReleaseRequest, SandboxLease, SandboxSpec
from sandbox.contract.version import CONTRACT_VERSION, SUPPORTED_MAJORS
from sandbox.registry.client import Registry


class ManagerActivities:
    def __init__(self, registry: Registry, provider=None) -> None:
        self.registry = registry
        self.provider = provider

    def all(self) -> list:
        return [self.acquire, self.release]

    @staticmethod
    def _lease(row: dict) -> SandboxLease:
        return SandboxLease(
            lease_id=row["lease_id"],
            vm_id=row["vm_id"],
            task_queue=names.vm_task_queue(row["vm_id"]),
            pool=row["pool"],
            contract_version=CONTRACT_VERSION,
            expires_at=row["lease_expires_at"],
        )

    @activity.defn(name=names.ACQUIRE)
    def acquire(self, spec: SandboxSpec) -> SandboxLease:
        info = activity.info()
        existing = self.registry.find_lease_by_request(spec.request_id)
        if existing is not None:
            return self._lease(existing)
        if spec.contract_major not in SUPPORTED_MAJORS:
            raise Incompatible(
                f"client contract major {spec.contract_major} not in {list(SUPPORTED_MAJORS)}"
            )
        lease_id = uuid.uuid4().hex
        row = self.registry.claim_idle(
            spec.pool,
            lease_id=lease_id,
            request_id=spec.request_id,
            owner_workflow_id=info.workflow_id,
            owner_run_id=info.workflow_run_id,
            hold_seconds=spec.hold_seconds,
            contract_major=spec.contract_major,
            labels=spec.labels,
        )
        if row is None:
            self.registry.record_pending(spec.request_id, spec.pool, info.workflow_id)
            raise NoCapacity(f"no idle VM in pool {spec.pool!r}")
        self.registry.fulfill_request(spec.request_id)
        self.registry.emit(
            "acquire",
            "manager",
            f"leased {row['vm_id']} to {info.workflow_id}",
            vm_id=row["vm_id"],
            details={"lease_id": lease_id, "request_id": spec.request_id},
        )
        return self._lease(row)

    @activity.defn(name=names.RELEASE)
    def release(self, req: ReleaseRequest) -> None:
        changed = self.registry.release(req.vm_id, req.lease_id, req.disposition)
        if not changed:
            return
        if req.disposition == "destroy":
            row = self.registry.get_vm(req.vm_id)
            ref = (row or {}).get("provider_ref", req.vm_id)
            if self.provider is not None:
                self.provider.terminate(ref)
            self.registry.set_state(req.vm_id, "terminated", reason="destroyed")
        self.registry.emit(
            "release",
            "manager",
            f"released {req.vm_id} ({req.disposition})",
            vm_id=req.vm_id,
            details={"lease_id": req.lease_id, "disposition": req.disposition},
        )
