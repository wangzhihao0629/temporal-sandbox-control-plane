"""Status API fixtures: a moto-backed registry and store, a stub provider, a fake owner lookup."""

import boto3
import pytest
from moto import mock_aws

from sandbox.objectstore import BUCKETS, ObjectStore
from sandbox.registry.client import Registry
from sandbox.registry.schema import create_tables
from sandbox.testing.stubs import StubProvider


class ListingProvider(StubProvider):
    """A stub whose `list()` returns what the test put there."""

    def __init__(self, instances=None):
        super().__init__()
        self.instances = list(instances or [])
        self.list_calls = 0

    def list(self):
        self.list_calls += 1
        return list(self.instances)


@pytest.fixture
def backend():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        create_tables(resource)
        store = ObjectStore(client=boto3.client("s3", region_name="us-east-1"))
        for bucket in BUCKETS:
            store.ensure_bucket(bucket)
        yield Registry(resource), store


@pytest.fixture
def owner_statuses():
    statuses: dict[tuple[str, str], str | None] = {}

    async def lookup(workflow_id: str, run_id: str) -> str | None:
        return statuses.get((workflow_id, run_id), statuses.get((workflow_id, "")))

    lookup.table = statuses
    return lookup


def seed_vm(registry, vm_id, state="idle", **lease):
    """Register a VM, move it to `state`, and lease it when lease fields are given."""
    registry.register_vm(vm_id, "demo", vm_id, "0.1.0", [1])
    if state != "booting":
        registry.set_state(vm_id, "idle", expect="booting", reason="ready")
    if lease:
        row = registry.claim_idle(
            "demo",
            lease_id=lease.get("lease_id", f"L-{vm_id}"),
            request_id=lease.get("request_id", f"R-{vm_id}"),
            owner_workflow_id=lease["owner_workflow_id"],
            owner_run_id=lease.get("owner_run_id", "run-1"),
            hold_seconds=1800,
            contract_major=1,
            labels={},
        )
        assert row and row["vm_id"] == vm_id
    elif state not in ("idle", "booting"):
        registry.set_state(vm_id, state, reason="test")
    return registry.get_vm(vm_id)
