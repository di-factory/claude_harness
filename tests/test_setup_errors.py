"""Setting up a first instance: every mistake met on a real setup fails early, with the fix."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from dif_general_harness.cli import main
from dif_general_harness.core.messages import Message
from dif_general_harness.providers.anthropic import AnthropicProvider, ProviderSetupError
from dif_general_harness.providers.base import ModelRequest
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.runtime.instance import InstanceError
from dif_general_harness.runtime.routing import RoutingError, check_anthropic_key
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tenancy import EnvSecrets
from dif_general_harness.tenancy.secrets import FileSecrets, SecretResolver


def test_spec_copy_takes_the_instance_files_along(
    examples: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = examples / "instances" / "clinica-sonrisa.json"
    dest = tmp_path / "clients" / "demo"
    assert main(["spec", "copy", str(source), str(dest), "--id", "demo-clinic"]) == 0
    copied = dest / "clinica-sonrisa" / "tpl_reminder.es-MX.md"
    assert (
        copied.read_text()
        == (source.parent / "clinica-sonrisa" / "tpl_reminder.es-MX.md").read_text()
    )
    resolved = load_instance(dest / "instance.json", PackCatalog(roots=[examples]))
    assert resolved.ok and resolved.spec.solution.id == "demo-clinic"
    assert main(["spec", "copy", str(source), str(dest)]) == 2  # never overwrites
    assert "already exists" in capsys.readouterr().err


def test_a_bare_copy_says_how_to_copy(examples: Path, tmp_path: Path) -> None:
    lone = tmp_path / "instance.json"
    shutil.copy(examples / "instances" / "clinica-sonrisa.json", lone)  # without its folder
    resolved = load_instance(lone, PackCatalog(roots=[examples]))
    [issue, *_] = [i for i in resolved.issues if i.code == "missing_file"]
    assert "spec copy" in issue.message


def test_an_instance_must_choose_its_models(examples: Path) -> None:
    path = examples / "instances" / "clinica-sonrisa.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["values"]["main_model"]
    path.write_text(json.dumps(data), encoding="utf-8")
    resolved = load_instance(path, PackCatalog(roots=[examples]))
    [issue] = [i for i in resolved.issues if i.code == "model_not_set"]
    assert '"main_model": "claude-sonnet-5-5"' in issue.message  # says what to write


async def test_missing_secrets_say_where_to_put_them(examples: Path, tmp_path: Path) -> None:
    resolved = load_instance(
        examples / "instances" / "clinica-sonrisa.json", PackCatalog(roots=[examples])
    )
    with pytest.raises(InstanceError) as raised:
        await Instance.open(resolved, RuntimeOptions(state_root=tmp_path, secrets=EnvSecrets({})))
    message = str(raised.value)
    assert "DIF_SECRET_ANTHROPIC" in message and "--secrets-dir" in message


def test_secret_files_forgive_whitespace_and_empty_means_unset(tmp_path: Path) -> None:
    (tmp_path / "anthropic").write_text("  sk-ant-api03-abc \r\n")
    (tmp_path / "twilio").write_text("")
    files = FileSecrets(tmp_path)
    assert files.get("anthropic") == "sk-ant-api03-abc"
    assert SecretResolver(files).backend.get("twilio") == ""
    assert SecretResolver(files).get("anthropic") == "sk-ant-api03-abc"
    with pytest.raises(LookupError):
        SecretResolver(files).get("twilio")
    assert EnvSecrets({"DIF_SECRET_LLM": "key\n"}).get("llm") == "key"


def test_anthropic_credentials_are_checked_at_start() -> None:
    with pytest.raises(RoutingError, match="subscription"):
        check_anthropic_key("sk-ant-oat01-xyz", {})
    check_anthropic_key("sk-ant-usr-xyz", {})  # some of these are tied to a workspace: try it
    check_anthropic_key("sk-ant-api03-xyz", {})
    check_anthropic_key("sk-ant-oat01-xyz", {"base_url": "https://gateway.internal"})
    provider = AnthropicProvider("claude-haiku-4-5", api_key="k", workspace_id="wrkspc_1")
    assert provider.client.default_headers["anthropic-workspace-id"] == "wrkspc_1"


async def test_a_key_without_a_workspace_gets_the_fix() -> None:
    import anthropic
    import httpx2 as httpx

    message = "This API key is not scoped to a workspace, so this request must include..."
    refused = anthropic.BadRequestError(
        message, response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
        body=None,
    )  # fmt: skip

    class Messages:
        def stream(self, **kw: object) -> object:
            raise refused

    class Client:
        messages = Messages()

        class beta:
            messages = Messages()

    provider = AnthropicProvider("claude-haiku-4-5", client=Client())  # type: ignore[arg-type]
    request = ModelRequest(system="", messages=[Message.user("hola")])
    with pytest.raises(ProviderSetupError, match="inside a workspace"):
        async for _ in provider.stream(request):
            pass


async def test_a_prompt_too_long_is_a_context_overflow() -> None:
    import anthropic
    import httpx2 as httpx

    from dif_general_harness.providers.base import ContextOverflow, ModelRequest

    refused = anthropic.BadRequestError(
        "prompt is too long: 213512 tokens > 200000 maximum",
        response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None,
    )  # fmt: skip

    class Messages:
        def stream(self, **kw: object) -> object:
            raise refused

    class Client:
        messages = Messages()

        class beta:
            messages = Messages()

    provider = AnthropicProvider("claude-haiku-4-5", client=Client())  # type: ignore[arg-type]
    request = ModelRequest(system="", messages=[Message.user("hola")])
    with pytest.raises(ContextOverflow, match="prompt is too long"):
        async for _ in provider.stream(request):
            pass
