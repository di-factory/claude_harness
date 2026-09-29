"""The container executor (M4.4): a throwaway, confined container per shell command."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from dif_general_harness.core.messages import ToolStatus, ToolUseBlock
from dif_general_harness.policy import AutoApprover
from dif_general_harness.providers import FakeProvider
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tenancy import EnvSecrets
from dif_general_harness.tools.packs import ContainerExecutor, executor_from_spec
from dif_general_harness.tools.packs.coding import ExecutorError

FAKE_RUNTIME = """#!/usr/bin/env bash
# A stand-in for docker: records its arguments, then plays the container.
echo "$*" >> "{log}"
if [ "$1" = "kill" ]; then exit 0; fi
command="${{@: -1}}"
case "$command" in
  sleep*) sleep 5 ;;
  *) echo "in container: $command"; exit 3 ;;
esac
"""


def _fake_runtime(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "runtime.log"
    script = tmp_path / "fake-docker"
    script.write_text(FAKE_RUNTIME.format(log=log))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


def test_the_container_is_confined(tmp_path: Path) -> None:
    ex = ContainerExecutor(image="python:3.12-slim", cpu=2, memory="4G")
    argv = ex.argv("pytest -q", tmp_path, "dif-exec-1")
    joined = " ".join(argv)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network none" in joined and "--cap-drop ALL" in joined
    assert "--read-only" in argv and "--security-opt no-new-privileges" in joined
    assert "--cpus 2 --memory 4g --pids-limit 256" in joined
    assert f"type=bind,source={tmp_path},target=/workspace" in joined
    assert argv[-5:] == ["--entrypoint", "bash", "python:3.12-slim", "-c", "pytest -q"]
    assert not any("SECRET" in a or "API_KEY" in a for a in argv)  # nothing of ours leaks in

    proxied = ContainerExecutor(
        image="img", allow_hosts=["pypi.org"], egress_proxy="http://egress:3128"
    ).argv("pip install x", tmp_path, "n")
    joined = " ".join(proxied)
    assert "--network dif-egress" in joined and "--network none" not in joined
    assert "HTTPS_PROXY=http://egress:3128" in joined and "DIF_ALLOW_HOSTS=pypi.org" in joined


def test_executor_from_spec() -> None:
    assert executor_from_spec(None) == (None, None)
    assert executor_from_spec({"type": "subprocess"}) == (None, None)
    ex, timeout = executor_from_spec(
        {
            "type": "container",
            "image": "sandbox:1",
            "network": "deny-by-default",
            "allow_hosts": ["pypi.org"],
            "cpu": 2,
            "memory": "4g",
            "timeout": "30m",
        }
    )
    assert isinstance(ex, ContainerExecutor) and timeout == 1800
    assert (ex.image, ex.cpu, ex.memory, ex.allow_hosts) == ("sandbox:1", 2.0, "4g", ["pypi.org"])
    for bad in (
        {"type": "container"},
        {"type": "container", "image": "{{var.sandbox_image}}"},
        {"type": "container", "image": "x", "memory": "lots"},
        {"type": "container", "image": "x", "network": "host"},
        {"type": "vm", "image": "x"},
    ):
        with pytest.raises(ExecutorError):
            executor_from_spec(bad)


async def test_commands_run_in_the_runtime_and_time_out(tmp_path: Path) -> None:
    runtime, log = _fake_runtime(tmp_path)
    ex = ContainerExecutor(image="img", runtime=str(runtime))
    done = await ex.run("make test", tmp_path, 10)
    assert (done.exit_code, done.output.strip()) == (3, "in container: make test")
    slow = await ex.run("sleep 60", tmp_path, 0.5)
    assert slow.timed_out
    calls = log.read_text().splitlines()
    name = calls[1].split("--name ")[1].split()[0]
    assert calls[-1] == f"kill {name}"  # the container itself is killed, not just the client


async def test_the_coding_pack_uses_the_workspace_executor(
    helper_solution: Path, tmp_path: Path
) -> None:
    runtime, log = _fake_runtime(tmp_path)
    pack = helper_solution.parent.parent / "packs" / "helper" / "pack.json"
    data = json.loads(pack.read_text())
    data["workspaces"]["repo"]["executor"] = {
        "type": "container",
        "image": "sandbox:1",
        "runtime": str(runtime),
        "timeout": "2m",
        "allow_hosts": ["pypi.org"],
    }
    pack.write_text(json.dumps(data))
    repo = tmp_path / "repo"
    repo.mkdir()
    resolved = load_instance(helper_solution, PackCatalog(roots=[pack.parent.parent]))
    options = RuntimeOptions(
        state_root=tmp_path / "state",
        secrets=EnvSecrets({}),
        workspaces={"repo": repo},
        provider=FakeProvider([]),
        approver=AutoApprover(),
    )
    async with await Instance.open(resolved, options) as inst:
        assert "egress_proxy_missing" in {i.code for i in inst.issues}
        result = await inst.agent().tools.execute(
            ToolUseBlock(id="1", name="coding.bash", input={"command": "ls"})
        )
        assert result.status is ToolStatus.OK and result.content["exit_code"] == 3
        assert "--entrypoint bash sandbox:1 -c ls" in log.read_text()
        assert "--network none" in log.read_text()  # no proxy: no network at all
