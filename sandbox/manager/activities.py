"""Acquire and release.

What: the two manager activities a workflow's client calls.
Why: acquire is idempotent on the caller's request_id, so a workflow that
crashes between claiming and recording finds its lease again. It never waits;
no capacity is a non-retryable NoCapacity that the client turns into a sleep
loop, and the pending request is recorded so it counts toward scale-out.
Release is idempotent on the lease id. A recycle only flips the row and the VM
finishes the wipe itself; a destroy terminates through the provider first, so a
failed terminate retries with the lease still held. The provider is required,
not optional, so a destroy can never silently skip termination and strand a VM
the registry has already marked gone.
Production: identical. These run as sync activities in a thread pool because
boto3 is blocking.
"""

import uuid

from temporalio import activity

from sandbox.contract import names
from sandbox.contract.errors import Incompatible, NoCapacity
from sandbox.contract.types import DISPOSITIONS, ReleaseRequest, SandboxLease, SandboxSpec
from sandbox.contract.version import CONTRACT_VERSION, SUPPORTED_MAJORS
from sandbox.registry.client import Registry


class ManagerActivities:
    def __init__(self, registry: Registry, provider) -> None:
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
            self.registry.fulfill_request(spec.request_id)
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
        if req.disposition not in DISPOSITIONS:
            raise Incompatible(f"unknown disposition {req.disposition!r}")
        if req.disposition == "destroy":
            # Terminate before releasing. The lease is what makes this activity
            # retryable: if the provider call fails with the row already unleased,
            # a retry finds nothing to do and the VM is stranded in `terminating`.
            row = self.registry.get_vm(req.vm_id)
            if row is None or row.get("lease_id") != req.lease_id:
                return
            self.provider.terminate(row.get("provider_ref", req.vm_id))
            if not self.registry.release(req.vm_id, req.lease_id, "destroy"):
                # Someone else released this lease between the read above and
                # here. The row belongs to them now; stamping `terminated` on it
                # would trample a lease this activity no longer holds.
                return
            self.registry.set_state(req.vm_id, "terminated", reason="destroyed")
        else:
            if not self.registry.release(req.vm_id, req.lease_id, req.disposition):
                return
        self.registry.emit(
            "release",
            "manager",
            f"released {req.vm_id} ({req.disposition})",
            vm_id=req.vm_id,
            details={"lease_id": req.lease_id, "disposition": req.disposition},
        )
