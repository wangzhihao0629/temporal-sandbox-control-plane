"""Create tables and buckets once the local endpoint answers.

A single `moto_server` process stands in for DynamoDB and S3 locally: on a
managed laptop, host processes can be confined to loopback, so real
DynamoDB Local and MinIO containers on the `container` bridge network are
unreachable from here even though a VM can reach the same host over that
bridge. moto answers both APIs on one port instead.

Usage: uv run python -m sandbox.bootstrap
Called by scripts/up.sh after moto starts.
Production: tables and buckets are created by Terraform; this module exists
for the local stack and tests.
"""

import os
import sys
import time
import urllib.error
import urllib.request

from sandbox import envfile
from sandbox.objectstore import BUCKETS, ObjectStore
from sandbox.registry.client import Registry
from sandbox.registry.schema import create_tables


def wait_http(url: str, timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except urllib.error.HTTPError:
            return  # any HTTP answer means the service is up
        except Exception:
            time.sleep(1)
    sys.exit(f"timed out waiting for {url}")


def main() -> None:
    envfile.load()
    wait_http(os.environ["DYNAMODB_ENDPOINT"])
    created = create_tables(Registry.resource_from_env())
    print(f"tables created: {created or 'none, already present'}")
    store = ObjectStore.from_env()
    for bucket in BUCKETS:
        store.ensure_bucket(bucket)
    print(f"buckets ready: {', '.join(BUCKETS)}")


if __name__ == "__main__":
    main()
