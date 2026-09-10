"""Integration fixtures: a real Temporal dev server, moto-backed registry and
object store, and in-process VMs."""

import shutil
import socket
import sys
import time
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from temporalio.testing import WorkflowEnvironment

from sandbox.objectstore import BUCKETS, ObjectStore
from sandbox.registry.client import Registry
from sandbox.registry.schema import create_tables

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def aws_server(monkeypatch):
    """moto as a real HTTP server, for tests whose subprocesses need to reach it.

    `mock_aws` patches boto3 inside this process only. The runner is a child
    process of the in-process VM, so it needs a real endpoint, and the VM agent
    hands every job the S3 identity it finds in its own environment.
    """
    from moto.server import ThreadedMotoServer

    port = _free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    try:
        endpoint = f"http://127.0.0.1:{port}"
        for key, value in {
            "S3_ENDPOINT": endpoint,
            "DYNAMODB_ENDPOINT": endpoint,
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_DEFAULT_REGION": "us-east-1",
        }.items():
            monkeypatch.setenv(key, value)
        s3 = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
        deadline = time.time() + 10
        while True:
            try:
                s3.list_buckets()
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.1)
        resource = boto3.resource("dynamodb", endpoint_url=endpoint, region_name="us-east-1")
        create_tables(resource)
        store = ObjectStore(endpoint_url=endpoint)
        for bucket in BUCKETS:
            store.ensure_bucket(bucket)
        yield Registry(resource), store
    finally:
        server.stop()


@pytest.fixture
def seed_root(tmp_path):
    from sandbox.runner.seed import make_bare_repo

    root = tmp_path / "repos"
    make_bare_repo(REPO_ROOT / "images" / "vm" / "seed" / "hello", root / "hello.git")
    return root


@pytest.fixture
def runner_artifact(aws_server, tmp_path):
    from sandbox.runner import package

    _, store = aws_server
    path, sha = package.build(REPO_ROOT, tmp_path / "dist")
    return package.publish(store, path, sha), sha


def session_params(session_id, runner_artifact, seed_root, workspace_root, **overrides):
    """CodingSessionParams for a test: the test profile, the host interpreter, temp seeds."""
    from sandbox.orchestrator.workflows import CodingSessionParams

    uri, sha = runner_artifact
    defaults = dict(
        session_id=session_id,
        runner_uri=uri,
        runner_sha256=sha,
        profile="test",
        turn_seconds=0,
        workspace_root=str(workspace_root),
        runner_env={"RUNNER_PYTHON": sys.executable, "SEED_REPOS_DIR": str(seed_root)},
    )
    return CodingSessionParams(**{**defaults, **overrides})
