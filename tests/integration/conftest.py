"""Integration fixtures: a real Temporal dev server, moto-backed registry and
object store, and in-process VMs."""

import shutil

import boto3
import pytest
from moto import mock_aws
from temporalio.testing import WorkflowEnvironment

from sandbox.objectstore import BUCKETS, ObjectStore
from sandbox.registry.client import Registry
from sandbox.registry.schema import create_tables


@pytest.fixture(scope="session")
async def env():
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=shutil.which("temporal")
    ) as environment:
        yield environment


@pytest.fixture
def aws():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        create_tables(resource)
        store = ObjectStore(client=boto3.client("s3", region_name="us-east-1"))
        for bucket in BUCKETS:
            store.ensure_bucket(bucket)
        yield Registry(resource), store
