"""The machine (GETTING_STARTED Phases 1 and 2) with one command.

``machine launch`` does what the guide asks an operator to click through in the AWS console,
and what Phase 2 types over SSH, so that a fresh server needs one command here and then
``./setup.sh`` on the box:

- a key pair (the private key is written locally, once; AWS never returns it again),
- a security group: HTTP and HTTPS from anywhere, SSH only from where the command runs
  (``--ssh-from``), never port 8080,
- an Ubuntu 24.04 LTS instance (Canonical's current image, read from the SSM public
  parameter), Graviton ``t4g.small`` where the region offers it, else ``t3.small``, 30 GB gp3,
  IMDSv2 only,
- an Elastic IP, so the address (and the webhooks pointing at it) never change,
- cloud-init user data that installs Docker, Caddy, git and uv, clones this repository as the
  ``ubuntu`` user and runs ``uv sync``; the login banner says when that finished.

Every step is idempotent: resources are found by their ``dif:machine`` tag (or name) and
reused, so the command can be run again after a failure or to read the address back.
``machine destroy`` takes the same resources down in the reverse order.

``boto3`` is an optional dependency (``.[aws]``); tests inject fake clients.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen

REPO_URL = "https://github.com/di-factory/claude_harness.git"
TAG = "dif:machine"
UBUNTU_AMI_PARAMETER = (
    "/aws/service/canonical/ubuntu/server/24.04/stable/current/{arch}/hvm/ebs-gp3/ami-id"
)
INSTANCE_TYPES = {"arm64": "t4g.small", "x86_64": "t3.small"}  # the guide's size, per arch
READY_MARKER = "/var/lib/dif-machine-ready"

USER_DATA = """#!/bin/bash
# dif-general-harness: GETTING_STARTED Phase 2, run once by cloud-init at first boot.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
cat > /etc/update-motd.d/99-dif <<'MOTD'
#!/bin/sh
if [ -f {marker} ]; then
  echo "dif-general-harness: the install finished. Next: cd claude_harness && ./setup.sh"
else
  echo "dif-general-harness: still installing (tail -f /var/log/cloud-init-output.log)"
fi
MOTD
chmod +x /etc/update-motd.d/99-dif
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 docker-buildx caddy git curl
usermod -aG docker ubuntu
sudo -iu ubuntu bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
for tool in uv uvx; do ln -sf /home/ubuntu/.local/bin/$tool /usr/local/bin/$tool; done
sudo -iu ubuntu bash -c 'git clone --branch {branch} {repo} claude_harness'
sudo -iu ubuntu bash -c 'cd claude_harness && uv sync -q'
sudo -iu ubuntu bash -c 'curl -fsSL https://claude.ai/install.sh | bash' || true
touch {marker}
"""


class MachineError(Exception):
    """A step that cannot continue; the message says what to do."""


@dataclass(frozen=True)
class MachinePlan:
    name: str = "dif-harness"
    ssh_from: str = "me"  # "me" (the public address of where this runs), a CIDR, or "anywhere"
    repo: str = REPO_URL
    branch: str = "main"
    disk_gb: int = 30
    instance_type: str | None = None  # default: t4g.small, or t3.small where t4g is not offered


@dataclass
class MachineInfo:
    name: str
    instance_id: str
    state: str
    ip: str | None
    key_file: Path | None
    instance_type: str = ""
    created: bool = False

    @property
    def ssh(self) -> str | None:
        if not self.ip or not self.key_file:
            return None
        return f"ssh -i {self.key_file} ubuntu@{self.ip}"


def user_data(repo: str = REPO_URL, branch: str = "main") -> str:
    return USER_DATA.format(repo=repo, branch=branch, marker=READY_MARKER)


def my_public_ip() -> str:
    """The public address of where this runs, as the guide's "SSH from your IP only"."""
    with urlopen("https://checkip.amazonaws.com", timeout=10) as response:
        ip = str(response.read().decode().strip())
    if not all(part.isdigit() for part in ip.split(".")) or ip.count(".") != 3:
        raise MachineError(f"could not read the public address (got {ip!r}); pass --ssh-from")
    return ip


def ssh_cidr(ssh_from: str, *, lookup: Callable[[], str] = my_public_ip) -> str:
    if ssh_from == "anywhere":
        return "0.0.0.0/0"
    if ssh_from == "me":
        return f"{lookup()}/32"
    if "/" not in ssh_from:
        return f"{ssh_from}/32"
    return ssh_from


class Launcher:
    """One machine, by name, in one region. ``ec2`` and ``ssm`` are boto3 clients."""

    def __init__(
        self,
        ec2: Any,
        ssm: Any,
        *,
        key_dir: Path,
        say: Callable[[str], None] = print,
        ip_lookup: Callable[[], str] = my_public_ip,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ec2 = ec2
        self.ssm = ssm
        self.key_dir = key_dir
        self.say = say
        self.ip_lookup = ip_lookup
        self.sleep = sleep

    # -- launch -----------------------------------------------------------------------------

    def launch(self, plan: MachinePlan) -> MachineInfo:
        key_file = self._key_pair(plan.name)
        vpc_id = self._default_vpc()
        instance_type = plan.instance_type or self._instance_type()
        arch = next((a for a, t in INSTANCE_TYPES.items() if t == instance_type), None)
        arch = arch or self._arch_of(instance_type)
        subnet_id = self._subnet(vpc_id, instance_type)
        ssh = ssh_cidr(plan.ssh_from, lookup=self.ip_lookup)
        sg_id = self._security_group(plan.name, vpc_id, ssh)
        existing = self._find_instance(plan.name)
        if existing:
            instance_id = existing["InstanceId"]
            self.say(f"instance {instance_id} exists ({existing['State']['Name']}); reusing it")
            created = False
        else:
            ami = self._ami(arch)
            self.say(f"launching {instance_type} ({arch}) from {ami} in {subnet_id}")
            run = self.ec2.run_instances(
                ImageId=ami,
                InstanceType=instance_type,
                MinCount=1,
                MaxCount=1,
                KeyName=plan.name,
                NetworkInterfaces=[
                    {
                        "DeviceIndex": 0,
                        "SubnetId": subnet_id,
                        "Groups": [sg_id],
                        "AssociatePublicIpAddress": True,
                    }
                ],
                BlockDeviceMappings=[
                    {
                        "DeviceName": "/dev/sda1",
                        "Ebs": {
                            "VolumeSize": plan.disk_gb,
                            "VolumeType": "gp3",
                            "DeleteOnTermination": True,
                        },
                    }
                ],
                MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
                UserData=user_data(plan.repo, plan.branch),
                TagSpecifications=[
                    _tags("instance", plan.name),
                    _tags("volume", plan.name),
                ],
            )
            instance_id = run["Instances"][0]["InstanceId"]
            created = True
        self._wait_running(instance_id)
        ip = self._elastic_ip(plan.name, instance_id)
        return MachineInfo(
            name=plan.name,
            instance_id=instance_id,
            state="running",
            ip=ip,
            key_file=key_file,
            instance_type=instance_type,
            created=created,
        )

    def status(self, name: str) -> MachineInfo | None:
        instance = self._find_instance(name)
        if not instance:
            return None
        address = self._find_address(name)
        key_file = self.key_dir / f"{name}.pem"
        return MachineInfo(
            name=name,
            instance_id=instance["InstanceId"],
            state=instance["State"]["Name"],
            ip=address.get("PublicIp") if address else instance.get("PublicIpAddress"),
            key_file=key_file if key_file.exists() else None,
            instance_type=instance.get("InstanceType", ""),
        )

    def destroy(self, name: str) -> list[str]:
        """Terminate the instance, release the address, delete the group and the key pair."""
        gone: list[str] = []
        instance = self._find_instance(name)
        if instance:
            instance_id = instance["InstanceId"]
            self.say(f"terminating {instance_id}")
            self.ec2.terminate_instances(InstanceIds=[instance_id])
            self.ec2.get_waiter("instance_terminated").wait(InstanceIds=[instance_id])
            gone.append(f"instance {instance_id}")
        address = self._find_address(name)
        if address:
            self.ec2.release_address(AllocationId=address["AllocationId"])
            gone.append(f"elastic ip {address.get('PublicIp', address['AllocationId'])}")
        for group in self.ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [name]}]
        )["SecurityGroups"]:
            for attempt in range(12):  # the interface of a terminated instance lingers a while
                try:
                    self.ec2.delete_security_group(GroupId=group["GroupId"])
                    break
                except Exception as exc:
                    if "DependencyViolation" not in str(exc) or attempt == 11:
                        raise
                    self.sleep(5)
            gone.append(f"security group {group['GroupId']}")
        if self._key_pair_exists(name):
            self.ec2.delete_key_pair(KeyName=name)
            gone.append(f"key pair {name}")
        key_file = self.key_dir / f"{name}.pem"
        if key_file.exists():
            key_file.unlink()
            gone.append(str(key_file))
        return gone

    # -- the pieces -------------------------------------------------------------------------

    def _key_pair_exists(self, name: str) -> bool:
        try:
            pairs = self.ec2.describe_key_pairs(KeyNames=[name])["KeyPairs"]
        except Exception as exc:
            if "InvalidKeyPair.NotFound" in str(exc):
                return False
            raise
        return bool(pairs)

    def _key_pair(self, name: str) -> Path:
        key_file = self.key_dir / f"{name}.pem"
        if self._key_pair_exists(name):
            if not key_file.exists():
                raise MachineError(
                    f"key pair {name!r} exists in AWS but {key_file} is not here (AWS never "
                    "returns a private key again): copy the .pem there, or delete the key pair "
                    "in AWS and run again"
                )
            return key_file
        self.say(f"creating key pair {name}")
        created = self.ec2.create_key_pair(
            KeyName=name, KeyType="ed25519", TagSpecifications=[_tags("key-pair", name)]
        )
        self.key_dir.mkdir(parents=True, exist_ok=True)
        key_file.write_text(created["KeyMaterial"], encoding="utf-8")
        os.chmod(key_file, 0o600)
        return key_file

    def _default_vpc(self) -> str:
        vpcs = self.ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
        if vpcs:
            return str(vpcs[0]["VpcId"])
        self.say("no default VPC in this region; creating it")
        return str(self.ec2.create_default_vpc()["Vpc"]["VpcId"])

    def _offered(self, instance_type: str, location_type: str = "region") -> list[str]:
        offers = self.ec2.describe_instance_type_offerings(
            LocationType=location_type,
            Filters=[{"Name": "instance-type", "Values": [instance_type]}],
        )["InstanceTypeOfferings"]
        return [str(o["Location"]) for o in offers]

    def _instance_type(self) -> str:
        for instance_type in INSTANCE_TYPES.values():
            if self._offered(instance_type):
                return instance_type
        raise MachineError(
            f"neither {' nor '.join(INSTANCE_TYPES.values())} is offered in this region; "
            "pass --type"
        )

    def _arch_of(self, instance_type: str) -> str:
        types = self.ec2.describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"]
        if not types:
            raise MachineError(f"unknown instance type {instance_type!r}")
        return str(types[0]["ProcessorInfo"]["SupportedArchitectures"][0])

    def _subnet(self, vpc_id: str, instance_type: str) -> str:
        zones = set(self._offered(instance_type, "availability-zone"))
        subnets = self.ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])[
            "Subnets"
        ]
        for subnet in sorted(subnets, key=lambda s: str(s["AvailabilityZone"])):
            if subnet["AvailabilityZone"] in zones:
                return str(subnet["SubnetId"])
        raise MachineError(f"no subnet of {vpc_id} is in a zone that offers {instance_type}")

    def _ami(self, arch: str) -> str:
        parameter = UBUNTU_AMI_PARAMETER.format(arch=arch)
        return str(self.ssm.get_parameter(Name=parameter)["Parameter"]["Value"])

    def _security_group(self, name: str, vpc_id: str, ssh: str) -> str:
        groups = self.ec2.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [name]},
                {"Name": "vpc-id", "Values": [vpc_id]},
            ]
        )["SecurityGroups"]
        if groups:
            sg_id = str(groups[0]["GroupId"])
        else:
            self.say(f"creating security group {name}")
            sg_id = str(
                self.ec2.create_security_group(
                    GroupName=name,
                    Description="dif-general-harness: web from anywhere, SSH from one place",
                    VpcId=vpc_id,
                    TagSpecifications=[_tags("security-group", name)],
                )["GroupId"]
            )
        rules = [
            _rule(80, ["0.0.0.0/0"], ["::/0"]),
            _rule(443, ["0.0.0.0/0"], ["::/0"]),
            _rule(22, [ssh] if ":" not in ssh else [], [ssh] if ":" in ssh else []),
        ]
        for rule in rules:
            try:
                self.ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[rule])
            except Exception as exc:
                if "InvalidPermission.Duplicate" not in str(exc):
                    raise
        return sg_id

    def _find_instance(self, name: str) -> dict[str, Any] | None:
        reservations = self.ec2.describe_instances(
            Filters=[
                {"Name": f"tag:{TAG}", "Values": [name]},
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                },
            ]
        )["Reservations"]
        instances = [i for r in reservations for i in r["Instances"]]
        return dict(instances[0]) if instances else None

    def _wait_running(self, instance_id: str) -> None:
        self.say(f"waiting for {instance_id} to run")
        self.ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

    def _find_address(self, name: str) -> dict[str, Any] | None:
        addresses = self.ec2.describe_addresses(Filters=[{"Name": f"tag:{TAG}", "Values": [name]}])[
            "Addresses"
        ]
        return dict(addresses[0]) if addresses else None

    def _elastic_ip(self, name: str, instance_id: str) -> str:
        address = self._find_address(name)
        if not address:
            self.say("allocating an elastic ip")
            address = self.ec2.allocate_address(
                Domain="vpc", TagSpecifications=[_tags("elastic-ip", name)]
            )
        if address.get("InstanceId") != instance_id:
            self.ec2.associate_address(
                AllocationId=address["AllocationId"],
                InstanceId=instance_id,
                AllowReassociation=True,
            )
        return str(address["PublicIp"])


def _tags(resource: str, name: str) -> dict[str, Any]:
    return {
        "ResourceType": resource,
        "Tags": [{"Key": "Name", "Value": name}, {"Key": TAG, "Value": name}],
    }


def _rule(port: int, v4: list[str], v6: list[str]) -> dict[str, Any]:
    return {
        "IpProtocol": "tcp",
        "FromPort": port,
        "ToPort": port,
        "IpRanges": [{"CidrIp": c} for c in v4],
        "Ipv6Ranges": [{"CidrIpv6": c} for c in v6],
    }


def clients(region: str) -> tuple[Any, Any]:
    """boto3's EC2 and SSM clients for ``region`` (boto3 is optional: ``.[aws]``)."""
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise MachineError("boto3 is not installed: uv sync --extra aws") from exc
    return boto3.client("ec2", region_name=region), boto3.client("ssm", region_name=region)


def explain(info: MachineInfo) -> str:
    lines = [f"{info.name}: {info.instance_id} ({info.instance_type or '?'}) is {info.state}"]
    if info.ip:
        lines.append(f"address: {info.ip} (elastic: it stays)")
    if info.ssh:
        lines.append(f"connect: {info.ssh}")
    elif info.ip and not info.key_file:
        lines.append("the private key for this machine is not on this computer")
    if info.created:
        lines.append(
            "the install (Docker, Caddy, uv, the repository) runs for a few minutes after boot;"
            " the login banner says when it finished. Then: cd claude_harness && ./setup.sh"
        )
    return "\n".join(lines)
