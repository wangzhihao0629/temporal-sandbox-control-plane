"""Object store: s3:// URI parsing and the bytes, file, and JSON round trips."""

import json

from sandbox.objectstore import ObjectStore


def test_parse_uri():
    assert ObjectStore.parse("s3://sandbox-jobs/a/b.txt") == ("sandbox-jobs", "a/b.txt")


def test_put_get_exists_and_list(s3_store):
    uri = "s3://sandbox-jobs/wf/j1/stdout.log"
    assert s3_store.exists(uri) is False
    s3_store.put_bytes(uri, b"hello")
    assert s3_store.get_bytes(uri) == b"hello"
    assert s3_store.exists(uri) is True
    assert s3_store.list("s3://sandbox-jobs/wf/j1/") == [uri]


def test_upload_and_download_file(s3_store, tmp_path):
    src = tmp_path / "in.bin"
    src.write_bytes(b"x" * 1000)
    size = s3_store.upload_file(src, "s3://sandbox-out/in.bin")
    assert size == 1000
    dst = tmp_path / "out.bin"
    assert s3_store.download_file("s3://sandbox-out/in.bin", dst) == 1000
    assert dst.read_bytes() == b"x" * 1000


def test_json_helpers_and_missing_object(s3_store):
    s3_store.put_json("s3://sandbox-out/s.json", {"a": 1})
    assert s3_store.get_json("s3://sandbox-out/s.json") == {"a": 1}
    assert json.loads(s3_store.get_bytes("s3://sandbox-out/s.json")) == {"a": 1}
    try:
        s3_store.get_bytes("s3://sandbox-out/missing")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")
