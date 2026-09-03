"""Shared test setup.

Every test runs with fake AWS credentials in the environment so boto3 never
looks for a real profile, and with moto active where a fixture asks for it.
"""

import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.pop("AWS_PROFILE", None)


@pytest.fixture
def s3_store():
    import boto3
    from moto import mock_aws

    from sandbox.objectstore import BUCKETS, ObjectStore

    with mock_aws():
        store = ObjectStore(client=boto3.client("s3", region_name="us-east-1"))
        for bucket in BUCKETS:
            store.ensure_bucket(bucket)
        yield store


@pytest.fixture
def registry():
    import boto3
    from moto import mock_aws

    from sandbox.registry.client import Registry
    from sandbox.registry.schema import create_tables

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        create_tables(resource)
        yield Registry(resource)
