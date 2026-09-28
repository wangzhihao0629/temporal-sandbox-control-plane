"""Object store access.

What: a thin boto3 S3 wrapper keyed by s3:// URIs.
Why: files, logs, artifacts, and envelopes never travel inside Temporal
payloads; they move through here and only URIs cross the wire. The endpoint
comes from the environment so the same code talks to the local moto server and
to S3 in production.
Production: identical, minus the endpoint override.
"""

import json
import os
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

BUCKETS = ("sandbox-artifacts", "sandbox-jobs", "sandbox-sessions", "sandbox-out")


class ObjectStore:
    def __init__(self, client=None, endpoint_url: str | None = None, region: str = "us-east-1"):
        self._s3 = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            config=Config(s3={"addressing_style": "path"}),
        )

    @classmethod
    def from_env(cls) -> "ObjectStore":
        return cls(
            endpoint_url=os.environ.get("S3_ENDPOINT") or None,
            region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )

    @staticmethod
    def parse(uri: str) -> tuple[str, str]:
        if not uri.startswith("s3://"):
            raise ValueError(f"not an s3 uri: {uri}")
        bucket, _, key = uri[5:].partition("/")
        return bucket, key

    def ensure_bucket(self, name: str) -> None:
        try:
            self._s3.head_bucket(Bucket=name)
        except ClientError:
            self._s3.create_bucket(Bucket=name)

    def put_bytes(
        self, uri: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        bucket, key = self.parse(uri)
        self._s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)

    def get_bytes(self, uri: str) -> bytes:
        bucket, key = self.parse(uri)
        try:
            return self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise FileNotFoundError(uri) from e
            raise

    def exists(self, uri: str) -> bool:
        bucket, key = self.parse(uri)
        try:
            self._s3.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def upload_file(self, path: str | os.PathLike, uri: str) -> int:
        bucket, key = self.parse(uri)
        file = Path(path)
        self._s3.upload_file(str(file), bucket, key)
        return file.stat().st_size

    def download_file(self, uri: str, path: str | os.PathLike) -> int:
        bucket, key = self.parse(uri)
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        self._s3.download_file(bucket, key, str(file))
        return file.stat().st_size

    def list(self, prefix_uri: str) -> list[str]:
        bucket, prefix = self.parse(prefix_uri)
        uris: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                uris.append(f"s3://{bucket}/{obj['Key']}")
        return uris

    def browse(self, bucket: str, prefix: str = "", limit: int = 500) -> dict:
        """One level of a bucket, the way a file browser shows a folder: the
        sub-prefixes under `prefix`, and the objects directly in it."""
        resp = self._s3.list_objects_v2(
            Bucket=bucket, Prefix=prefix, Delimiter="/", MaxKeys=limit
        )
        return {
            "prefixes": [p["Prefix"] for p in resp.get("CommonPrefixes", [])],
            "objects": [
                {
                    "key": o["Key"],
                    "size": o["Size"],
                    "modified": o["LastModified"].isoformat(),
                }
                for o in resp.get("Contents", [])
            ],
            "truncated": resp.get("IsTruncated", False),
        }

    def usage(self, bucket: str) -> tuple[int, int]:
        """How many objects the bucket holds, and their total size in bytes."""
        count = size = 0
        for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                count += 1
                size += obj["Size"]
        return count, size

    def put_json(self, uri: str, obj) -> None:
        self.put_bytes(uri, json.dumps(obj, indent=2).encode(), content_type="application/json")

    def get_json(self, uri: str):
        return json.loads(self.get_bytes(uri))
