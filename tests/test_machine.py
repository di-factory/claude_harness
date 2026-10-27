"""The machine with one command (GETTING_STARTED Phases 1 and 2): offline, on a fake EC2."""

from __future__ import annotations

import argparse
import stat
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.cli import _machine, main
from dif_general_harness.constructor import machine
from dif_general_harness.constructor.machine import (
    READY_MARKER,
    TAG,
    Launcher,
    MachineError,
    MachinePlan,
    ssh_cidr,
    user_data,
)


class _Waiter:
    def __init__(self, ec2: FakeEC2, name: str) -> None:
        self.ec2, self.name = ec2, name

    def wait(self, **kwargs: Any) -> None:
        self.ec2.calls.append((f"wait:{self.name}", kwargs))
        if self.name == "instance_terminated":
            for instance in self.ec2.instances.values():
                if instance["InstanceId"] in kwargs["InstanceIds"]:
                    instance["State"] = {"Name": "terminated"}


class FakeEC2:
    """The slice of boto3's EC2 client the launcher uses, remembering what was asked."""

    def __init__(self, *, offered: tuple[str, ...] = ("t4g.small", "t3.small")) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.offered = set(offered)
        self.key_pairs: dict[str, str] = {}
        self.vpcs: list[dict[str, Any]] = [{"VpcId": "vpc-default", "IsDefault": True}]
        self.subnets = [
            {"SubnetId": "subnet-b", "AvailabilityZone": "mx-central-1b", "VpcId": "vpc-default"},
            {"SubnetId": "subnet-a", "AvailabilityZone": "mx-central-1a", "VpcId": "vpc-default"},
        ]
        self.groups: dict[str, dict[str, Any]] = {}
        self.rules: dict[str, list[dict[str, Any]]] = {}
        self.instances: dict[str, dict[str, Any]] = {}
        self.addresses: dict[str, dict[str, Any]] = {}
        self.dependency_violations = 0

    def _note(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    # key pairs
    def describe_key_pairs(self, KeyNames: list[str]) -> dict[str, Any]:
        self._note("describe_key_pairs", KeyNames=KeyNames)
        if KeyNames[0] not in self.key_pairs:
            raise RuntimeError("InvalidKeyPair.NotFound: no such key")
        return {"KeyPairs": [{"KeyName": KeyNames[0]}]}

    def create_key_pair(self, KeyName: str, **kwargs: Any) -> dict[str, Any]:
        self._note("create_key_pair", KeyName=KeyName, **kwargs)
        self.key_pairs[KeyName] = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n"
        return {"KeyMaterial": self.key_pairs[KeyName]}

    def delete_key_pair(self, KeyName: str) -> None:
        self._note("delete_key_pair", KeyName=KeyName)
        self.key_pairs.pop(KeyName)

    # network
    def describe_vpcs(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        self._note("describe_vpcs", Filters=Filters)
        return {"Vpcs": [v for v in self.vpcs if v["IsDefault"]]}

    def create_default_vpc(self) -> dict[str, Any]:
        self._note("create_default_vpc")
        self.vpcs.append({"VpcId": "vpc-new", "IsDefault": True})
        self.subnets = [
            {"SubnetId": "subnet-new", "AvailabilityZone": "mx-central-1a", "VpcId": "vpc-new"}
        ]
        return {"Vpc": {"VpcId": "vpc-new"}}

    def describe_subnets(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        self._note("describe_subnets", Filters=Filters)
        vpc = Filters[0]["Values"][0]
        return {"Subnets": [s for s in self.subnets if s["VpcId"] == vpc]}

    def describe_instance_type_offerings(
        self, LocationType: str, Filters: list[dict[str, Any]]
    ) -> dict[str, Any]:
        wanted = Filters[0]["Values"][0]
        if wanted not in self.offered:
            return {"InstanceTypeOfferings": []}
        if LocationType == "region":
            return {"InstanceTypeOfferings": [{"Location": "mx-central-1"}]}
        return {"InstanceTypeOfferings": [{"Location": "mx-central-1a"}]}  # not in zone b

    def describe_instance_types(self, InstanceTypes: list[str]) -> dict[str, Any]:
        arch = "arm64" if InstanceTypes[0].startswith(("t4g", "m7g", "c7g")) else "x86_64"
        return {"InstanceTypes": [{"ProcessorInfo": {"SupportedArchitectures": [arch]}}]}

    def describe_security_groups(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        self._note("describe_security_groups", Filters=Filters)
        name = Filters[0]["Values"][0]
        return {"SecurityGroups": [g for g in self.groups.values() if g["GroupName"] == name]}

    def create_security_group(self, GroupName: str, VpcId: str, **kwargs: Any) -> dict[str, Any]:
        self._note("create_security_group", GroupName=GroupName, VpcId=VpcId, **kwargs)
        sg_id = f"sg-{len(self.groups) + 1}"
        self.groups[sg_id] = {"GroupId": sg_id, "GroupName": GroupName, "VpcId": VpcId}
        self.rules[sg_id] = []
        return {"GroupId": sg_id}

    def authorize_security_group_ingress(
        self, GroupId: str, IpPermissions: list[dict[str, Any]]
    ) -> None:
        self._note("authorize_security_group_ingress", GroupId=GroupId, IpPermissions=IpPermissions)
        for rule in IpPermissions:
            if rule in self.rules[GroupId]:
                raise RuntimeError("InvalidPermission.Duplicate: the rule already exists")
            self.rules[GroupId].append(rule)

    def delete_security_group(self, GroupId: str) -> None:
        self._note("delete_security_group", GroupId=GroupId)
        if self.dependency_violations:
            self.dependency_violations -= 1
            raise RuntimeError("DependencyViolation: resource has a dependent object")
        self.groups.pop(GroupId)

    # instances
    def describe_instances(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        self._note("describe_instances", Filters=Filters)
        name = Filters[0]["Values"][0]
        states = Filters[1]["Values"]
        found = [
            i
            for i in self.instances.values()
            if i["Tags"].get(TAG) == name and i["State"]["Name"] in states
        ]
        return {"Reservations": [{"Instances": found}] if found else []}

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        self._note("run_instances", **kwargs)
        instance_id = f"i-{len(self.instances) + 1}"
        tags = {
            t["Key"]: t["Value"]
            for spec in kwargs["TagSpecifications"]
            if spec["ResourceType"] == "instance"
            for t in spec["Tags"]
        }
        self.instances[instance_id] = {
            "InstanceId": instance_id,
            "State": {"Name": "pending"},
            "InstanceType": kwargs["InstanceType"],
            "Tags": tags,
            "PublicIpAddress": "3.3.3.3",
        }
        return {"Instances": [self.instances[instance_id]]}

    def terminate_instances(self, InstanceIds: list[str]) -> None:
        self._note("terminate_instances", InstanceIds=InstanceIds)
        for instance_id in InstanceIds:
            self.instances[instance_id]["State"] = {"Name": "shutting-down"}

    def get_waiter(self, name: str) -> _Waiter:
        if name == "instance_running":
            for instance in self.instances.values():
                if instance["State"]["Name"] == "pending":
                    instance["State"] = {"Name": "running"}
        return _Waiter(self, name)

    # addresses
    def describe_addresses(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        self._note("describe_addresses", Filters=Filters)
        name = Filters[0]["Values"][0]
        return {"Addresses": [a for a in self.addresses.values() if a["Tags"].get(TAG) == name]}

    def allocate_address(
        self, Domain: str, TagSpecifications: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self._note("allocate_address", Domain=Domain, TagSpecifications=TagSpecifications)
        allocation = f"eipalloc-{len(self.addresses) + 1}"
        self.addresses[allocation] = {
            "AllocationId": allocation,
            "PublicIp": "52.0.0.7",
            "Tags": {t["Key"]: t["Value"] for t in TagSpecifications[0]["Tags"]},
        }
        return dict(self.addresses[allocation])

    def associate_address(self, AllocationId: str, InstanceId: str, **kwargs: Any) -> None:
        self._note("associate_address", AllocationId=AllocationId, InstanceId=InstanceId, **kwargs)
        self.addresses[AllocationId]["InstanceId"] = InstanceId

    def release_address(self, AllocationId: str) -> None:
        self._note("release_address", AllocationId=AllocationId)
        self.addresses.pop(AllocationId)


class FakeSSM:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def get_parameter(self, Name: str) -> dict[str, Any]:
        self.asked.append(Name)
        arch = Name.split("/current/")[1].split("/")[0]
        return {"Parameter": {"Value": f"ami-ubuntu-{arch}"}}


def _launcher(
    tmp_path: Path, ec2: FakeEC2 | None = None, ssm: FakeSSM | None = None
) -> tuple[Launcher, FakeEC2, FakeSSM, list[str]]:
    ec2 = ec2 or FakeEC2()
    ssm = ssm or FakeSSM()
    said: list[str] = []
    launcher = Launcher(
        ec2,
        ssm,
        key_dir=tmp_path / "machines",
        say=said.append,
        ip_lookup=lambda: "203.0.113.9",
        sleep=lambda _s: None,
    )
    return launcher, ec2, ssm, said


def test_launch_builds_the_guides_machine(tmp_path: Path) -> None:
    launcher, ec2, ssm, _ = _launcher(tmp_path)
    info = launcher.launch(MachinePlan(name="clinica"))

    assert info.created and info.state == "running" and info.ip == "52.0.0.7"
    assert info.instance_type == "t4g.small"
    assert info.ssh == f"ssh -i {tmp_path / 'machines' / 'clinica.pem'} ubuntu@52.0.0.7"
    # the private key is on disk, for its owner only
    key_file = tmp_path / "machines" / "clinica.pem"
    assert key_file.read_text().startswith("-----BEGIN")
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    # Canonical's current Ubuntu 24.04 image for the instance's architecture
    assert ssm.asked == [
        "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
    ]
    run = next(kw for name, kw in ec2.calls if name == "run_instances")
    assert run["ImageId"] == "ami-ubuntu-arm64" and run["InstanceType"] == "t4g.small"
    assert run["KeyName"] == "clinica"
    # 30 GB gp3, IMDSv2 only, a subnet in a zone that offers the type (b does not)
    assert run["BlockDeviceMappings"][0]["Ebs"] == {
        "VolumeSize": 30,
        "VolumeType": "gp3",
        "DeleteOnTermination": True,
    }
    assert run["MetadataOptions"]["HttpTokens"] == "required"
    assert run["NetworkInterfaces"][0]["SubnetId"] == "subnet-a"
    assert run["NetworkInterfaces"][0]["Groups"] == ["sg-1"]
    assert {t["Key"]: t["Value"] for t in run["TagSpecifications"][0]["Tags"]} == {
        "Name": "clinica",
        TAG: "clinica",
    }
    # the security group: web from anywhere, SSH from this computer only, never 8080
    ports = {(r["FromPort"], tuple(x["CidrIp"] for x in r["IpRanges"])) for r in ec2.rules["sg-1"]}
    assert ports == {(80, ("0.0.0.0/0",)), (443, ("0.0.0.0/0",)), (22, ("203.0.113.9/32",))}
    # the elastic ip is tagged and attached
    assert ec2.addresses["eipalloc-1"]["InstanceId"] == "i-1"
    assert ec2.addresses["eipalloc-1"]["Tags"][TAG] == "clinica"
    # Phase 2 runs at first boot as the ubuntu user, and the login banner reports it
    data = run["UserData"]
    assert data.startswith("#!/bin/bash")
    for needle in (
        "docker.io docker-compose-v2 docker-buildx caddy git curl",
        "usermod -aG docker ubuntu",
        "astral.sh/uv/install.sh",
        "git clone --branch main https://github.com/di-factory/claude_harness.git claude_harness",
        "uv sync -q",
        "update-motd.d/99-dif",
        f"touch {READY_MARKER}",
    ):
        assert needle in data
    assert "8080" not in data


def test_launch_again_reuses_everything(tmp_path: Path) -> None:
    launcher, ec2, _, said = _launcher(tmp_path)
    first = launcher.launch(MachinePlan(name="clinica"))
    before = len(ec2.calls)
    again = launcher.launch(MachinePlan(name="clinica"))
    creating = {
        name
        for name, _ in ec2.calls[before:]
        if name in {"create_key_pair", "create_security_group", "run_instances", "allocate_address"}
    }
    assert creating == set()
    assert not again.created
    assert again.instance_id == first.instance_id and again.ip == first.ip
    assert again.key_file == first.key_file
    assert any("exists (running); reusing it" in line for line in said)
    # a duplicate ingress rule is not an error
    assert len(ec2.rules["sg-1"]) == 3


def test_falls_back_to_x86_where_graviton_is_not_offered(tmp_path: Path) -> None:
    launcher, ec2, ssm, _ = _launcher(tmp_path, FakeEC2(offered=("t3.small",)))
    info = launcher.launch(MachinePlan(name="m"))
    assert info.instance_type == "t3.small"
    assert ssm.asked[0].endswith("/x86_64/hvm/ebs-gp3/ami-id")
    run = next(kw for name, kw in ec2.calls if name == "run_instances")
    assert run["ImageId"] == "ami-ubuntu-x86_64"


def test_nothing_offered_says_so(tmp_path: Path) -> None:
    launcher, _, _, _ = _launcher(tmp_path, FakeEC2(offered=()))
    with pytest.raises(MachineError, match=r"neither t4g\.small nor t3\.small"):
        launcher.launch(MachinePlan(name="m"))


def test_chosen_type_and_arch_and_no_default_vpc(tmp_path: Path) -> None:
    ec2 = FakeEC2(offered=("m7g.medium",))
    ec2.vpcs = []
    launcher, ec2, ssm, _ = _launcher(tmp_path, ec2)
    info = launcher.launch(MachinePlan(name="m", instance_type="m7g.medium", disk_gb=50))
    assert info.instance_type == "m7g.medium"
    assert ssm.asked[0].endswith("/arm64/hvm/ebs-gp3/ami-id")
    assert "create_default_vpc" in ec2.names()
    run = next(kw for name, kw in ec2.calls if name == "run_instances")
    assert run["NetworkInterfaces"][0]["SubnetId"] == "subnet-new"
    assert run["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] == 50


def test_key_pair_without_its_file_is_refused(tmp_path: Path) -> None:
    launcher, ec2, _, _ = _launcher(tmp_path)
    launcher.launch(MachinePlan(name="m"))
    (tmp_path / "machines" / "m.pem").unlink()
    with pytest.raises(MachineError, match="AWS never returns a private key again"):
        launcher.launch(MachinePlan(name="m"))
    assert ec2.names().count("run_instances") == 1


def test_ssh_from() -> None:
    assert ssh_cidr("anywhere") == "0.0.0.0/0"
    assert ssh_cidr("me", lookup=lambda: "198.51.100.4") == "198.51.100.4/32"
    assert ssh_cidr("198.51.100.4") == "198.51.100.4/32"
    assert ssh_cidr("10.0.0.0/8") == "10.0.0.0/8"
    with pytest.raises(MachineError, match="pass --ssh-from"):
        machine.ssh_cidr("me", lookup=lambda: _bad_ip())


def _bad_ip() -> str:
    raise MachineError("could not read the public address (got '<html>'); pass --ssh-from")


def test_status_and_destroy(tmp_path: Path) -> None:
    launcher, ec2, _, _ = _launcher(tmp_path)
    assert launcher.status("m") is None
    launcher.launch(MachinePlan(name="m"))
    info = launcher.status("m")
    assert info is not None and info.state == "running" and info.ip == "52.0.0.7"
    assert info.ssh is not None and not info.created

    ec2.dependency_violations = 2  # the interface lingers after termination
    gone = launcher.destroy("m")
    assert gone == [
        "instance i-1",
        "elastic ip 52.0.0.7",
        "security group sg-1",
        "key pair m",
        str(tmp_path / "machines" / "m.pem"),
    ]
    names = ec2.names()
    assert names.index("terminate_instances") < names.index("wait:instance_terminated")
    assert names.index("wait:instance_terminated") < names.index("release_address")
    assert names.count("delete_security_group") == 3
    assert ec2.instances["i-1"]["State"]["Name"] == "terminated"
    assert not ec2.addresses and not ec2.groups and not ec2.key_pairs
    assert not (tmp_path / "machines" / "m.pem").exists()
    assert launcher.status("m") is None
    assert launcher.destroy("m") == []


def test_user_data_takes_another_repository_and_branch() -> None:
    data = user_data("https://example.com/fork.git", "work")
    assert "git clone --branch work https://example.com/fork.git claude_harness" in data


def test_cli_needs_a_region(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["machine", "status", "--region", ""]) == 2
    assert "which region?" in capsys.readouterr().err


def test_cli_commands_on_a_fake(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    launcher, _, _, _ = _launcher(tmp_path)
    base = {"region": "mx-central-1", "name": "m", "key_dir": tmp_path / "machines"}
    launch = argparse.Namespace(
        group="machine",
        command="launch",
        ssh_from="anywhere",
        repo=machine.REPO_URL,
        branch="main",
        disk=30,
        instance_type=None,
        **base,
    )
    assert _machine(launch, launcher) == 0
    out = capsys.readouterr().out
    assert "m: i-1 (t4g.small) is running" in out
    assert "address: 52.0.0.7" in out and "ssh -i" in out and "./setup.sh" in out

    assert _machine(argparse.Namespace(group="machine", command="status", **base), launcher) == 0
    assert "is running" in capsys.readouterr().out

    destroy = argparse.Namespace(group="machine", command="destroy", yes=True, **base)
    assert _machine(destroy, launcher) == 0
    assert "removed instance i-1" in capsys.readouterr().out
    assert _machine(argparse.Namespace(group="machine", command="status", **base), launcher) == 1
    assert "no machine named 'm'" in capsys.readouterr().out
