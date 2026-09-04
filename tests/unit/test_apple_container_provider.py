"""Apple container provider: parsing, CLI shapes, and terminate's missing-container ruling."""

import json
import shutil

import pytest

from sandbox.manager.providers.apple_container import (
    AppleContainerProvider,
    ProviderError,
    parse_instance,
)
from sandbox.manager.providers.base import LaunchSpec

RAW = {
    "configuration": {
        "id": "sbx-3f9c1a2b",
        "creationDate": "2026-09-03T20:00:40Z",
        "image": {"reference": "sandbox-vm:dev"},
        "labels": {},
    },
    "id": "sbx-3f9c1a2b",
    "status": {
        "networks": [
            {
                "hostname": "sbx-3f9c1a2b",
                "ipv4Address": "192.168.64.3/24",
                "ipv4Gateway": "192.168.64.1",
                "network": "default",
            }
        ],
        "startedDate": "2026-09-03T20:00:41Z",
        "state": "running",
    },
}

INFRA = {
    "configuration": {"id": "sandbox-minio", "creationDate": "2026-09-03T19:00:00Z"},
    "id": "sandbox-minio",
    "status": {
        "networks": [{"ipv4Address": "192.168.64.2/24", "ipv4Gateway": "192.168.64.1"}],
        "state": "running",
    },
}


class FakeCli:
    def __init__(self, responses=None, fail_on=()):
        self.calls: list[list[str]] = []
        self.responses = responses or {}
        self.fail_on = fail_on

    def __call__(self, args):
        self.calls.append(args)
        if args[0] in self.fail_on:
            raise ProviderError(f"container {args[0]} failed")
        return self.responses.get(args[0], "")


def test_parse_instance_reads_address_and_gateway():
    inst = parse_instance(RAW)
    assert inst.vm_id == inst.provider_ref == "sbx-3f9c1a2b"
    assert inst.state == "running"
    assert inst.address == "192.168.64.3" and inst.gateway == "192.168.64.1"
    assert inst.created_at == "2026-09-03T20:00:40Z"


def test_parse_instance_tolerates_a_stopped_container_without_networks():
    inst = parse_instance({"configuration": {"id": "sbx-x"}, "status": {"state": "stopped"}})
    assert inst.address == "" and inst.state == "stopped"


def test_launch_builds_the_run_command():
    cli = FakeCli()
    provider = AppleContainerProvider(image="sandbox-vm:dev", runner=cli)
    ref = provider.launch(
        "sbx-abc",
        LaunchSpec(image="sandbox-vm:dev", cpus=2, memory="2048M"),
        {"VM_ID": "sbx-abc", "POOL": "demo"},
    )
    assert ref == "sbx-abc"
    assert cli.calls == [
        [
            "run",
            "--detach",
            "--init",
            "--name",
            "sbx-abc",
            "--cpus",
            "2",
            "--memory",
            "2048M",
            "--env",
            "VM_ID=sbx-abc",
            "--env",
            "POOL=demo",
            "sandbox-vm:dev",
        ]
    ]


def test_list_filters_to_sandbox_vms_and_list_all_does_not():
    cli = FakeCli(responses={"ls": json.dumps([RAW, INFRA])})
    provider = AppleContainerProvider(image="sandbox-vm:dev", runner=cli)
    assert [i.vm_id for i in provider.list()] == ["sbx-3f9c1a2b"]
    assert [i.vm_id for i in provider.list_all()] == ["sbx-3f9c1a2b", "sandbox-minio"]
    assert provider.gateway() == "192.168.64.1"
    assert cli.calls[0] == ["ls", "--all", "--format", "json"]


def test_describe_returns_none_when_the_container_is_gone():
    cli = FakeCli(responses={"inspect": json.dumps([RAW])}, fail_on=())
    provider = AppleContainerProvider(image="sandbox-vm:dev", runner=cli)
    assert provider.describe("sbx-3f9c1a2b").address == "192.168.64.3"
    gone = AppleContainerProvider(image="sandbox-vm:dev", runner=FakeCli(fail_on=("inspect",)))
    assert gone.describe("sbx-missing") is None


def test_terminate_kill_and_stop_commands():
    cli = FakeCli()
    provider = AppleContainerProvider(image="sandbox-vm:dev", runner=cli)
    provider.terminate("sbx-a")
    provider.kill("sbx-a")
    provider.stop("sbx-a")
    assert cli.calls == [
        ["delete", "--force", "sbx-a"],
        ["kill", "--signal", "KILL", "sbx-a"],
        ["stop", "--time", "20", "sbx-a"],
    ]


def test_terminate_treats_a_missing_container_as_success():
    cli = FakeCli(fail_on=("delete", "inspect"))
    provider = AppleContainerProvider(image="sandbox-vm:dev", runner=cli)
    provider.terminate("sbx-gone")
    assert cli.calls == [["delete", "--force", "sbx-gone"], ["inspect", "sbx-gone"]]


def test_terminate_reraises_when_the_container_still_exists():
    cli = FakeCli(fail_on=("delete",), responses={"inspect": json.dumps([RAW])})
    provider = AppleContainerProvider(image="sandbox-vm:dev", runner=cli)
    with pytest.raises(ProviderError):
        provider.terminate("sbx-3f9c1a2b")
    assert cli.calls == [["delete", "--force", "sbx-3f9c1a2b"], ["inspect", "sbx-3f9c1a2b"]]


@pytest.mark.skipif(shutil.which("container") is None, reason="container CLI not installed")
def test_run_cli_raises_provider_error_on_nonzero_exit():
    from sandbox.manager.providers.apple_container import run_cli

    with pytest.raises(ProviderError):
        run_cli(["definitely-not-a-subcommand"])
