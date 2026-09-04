"""Create tables and buckets once the local endpoints answer.

Usage: uv run python -m sandbox.bootstrap
Called by scripts/up.sh after the infra containers start.
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
    wait_http(os.environ["S3_ENDPOINT"] + "/minio/health/live")
    created = create_tables(Registry.resource_from_env())
    print(f"tables created: {created or 'none, already present'}")
    store = ObjectStore.from_env()
    for bucket in BUCKETS:
        store.ensure_bucket(bucket)
    print(f"buckets ready: {', '.join(BUCKETS)}")


if __name__ == "__main__":
    main()
