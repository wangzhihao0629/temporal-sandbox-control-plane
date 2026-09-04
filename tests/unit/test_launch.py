"""VM id scheme, launch spec, and boot environment."""

import re

from sandbox.manager.launch import launch_spec, new_vm_id, vm_environment

FAKE_ENV = {
    "VM_TEMPORAL_ADDRESS": "192.168.64.1:7233",
    "DYNAMODB_ENDPOINT": "http://127.0.0.1:5000",
    "S3_ENDPOINT": "http://127.0.0.1:5000",
    "VM_DYNAMODB_ENDPOINT": "http://192.168.64.1:5000",
    "VM_S3_ENDPOINT": "http://192.168.64.1:5000",
    "AWS_ACCESS_KEY_ID": "local",
    "AWS_SECRET_ACCESS_KEY": "localsecret",
    "SANDBOX_VM_IMAGE": "sandbox-vm:dev",
}


def test_new_vm_id_has_the_sbx_prefix_and_eight_hex_chars():
    assert re.fullmatch(r"sbx-[0-9a-f]{8}", new_vm_id())
    assert new_vm_id() != new_vm_id()


def test_launch_spec_defaults():
    spec = launch_spec(FAKE_ENV)
    assert (spec.image, spec.cpus, spec.memory) == ("sandbox-vm:dev", 2, "2048M")


def test_vm_environment_carries_identity_endpoints_and_the_agent_user():
    env = vm_environment("sbx-abc", "demo", FAKE_ENV)
    assert env["VM_ID"] == "sbx-abc" and env["POOL"] == "demo"
    assert env["TEMPORAL_ADDRESS"] == "192.168.64.1:7233"
    assert env["DYNAMODB_ENDPOINT"] == "http://192.168.64.1:5000"
    assert env["S3_ENDPOINT"] == "http://192.168.64.1:5000"
    assert env["SANDBOX_RUN_AS_USER"] == "agent"
    assert env["AWS_ACCESS_KEY_ID"] == "local"
    assert "TEMPORAL_UI" not in env


def test_vm_environment_falls_back_to_the_host_endpoints_without_vm_specific_ones():
    env_without_vm_endpoints = {
        k: v for k, v in FAKE_ENV.items() if k not in ("VM_DYNAMODB_ENDPOINT", "VM_S3_ENDPOINT")
    }
    env = vm_environment("sbx-abc", "demo", env_without_vm_endpoints)
    assert env["DYNAMODB_ENDPOINT"] == "http://127.0.0.1:5000"
    assert env["S3_ENDPOINT"] == "http://127.0.0.1:5000"
