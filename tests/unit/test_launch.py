"""VM id scheme, launch spec, and boot environment."""

import re

from sandbox.manager.launch import launch_spec, new_vm_id, vm_environment

FAKE_ENV = {
    "VM_TEMPORAL_ADDRESS": "192.168.64.1:7233",
    "DYNAMODB_ENDPOINT": "http://192.168.64.2:8000",
    "S3_ENDPOINT": "http://192.168.64.3:9000",
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
    assert env["DYNAMODB_ENDPOINT"] == "http://192.168.64.2:8000"
    assert env["S3_ENDPOINT"] == "http://192.168.64.3:9000"
    assert env["SANDBOX_RUN_AS_USER"] == "agent"
    assert env["AWS_ACCESS_KEY_ID"] == "local"
    assert "TEMPORAL_UI" not in env
