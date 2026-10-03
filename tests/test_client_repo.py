"""Each client is its own git repository, made from ``templates/client``: every setup step is
a commit, it is published as a private GitHub repository when a token is stored, and at the
handover it moves to the client. GitHub is a ``MockTransport``; pushes go to a local bare
repository."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx2
import pytest

from dif_general_harness.cli import main
from dif_general_harness.constructor import client_repo
from dif_general_harness.constructor.client_repo import GitHub
from dif_general_harness.constructor.setup import Setup
from dif_general_harness.core.messages import Message
from dif_general_harness.providers import FakeProvider
from tests.test_setup_wizard import KEY, _replies

ID = "clinica-sonrisa-pyme-appointment-agent"
URL = "https://3-148-79-116.sslip.io"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DIF_SECRETS_DIR", raising=False)
    monkeypatch.delenv("DIF_SECRET_ANTHROPIC", raising=False)
    monkeypatch.delenv("DIF_SECRET_GITHUB", raising=False)
    return tmp_path / "home"


def _git(folder: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True,
                          check=True).stdout  # fmt: skip


def _log(folder: Path) -> list[str]:
    return _git(folder, "log", "--format=%s").splitlines()


class FakeGitHub:
    """The GitHub API: the repository does not exist until it is created."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.created = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["Authorization"] == "Bearer ghp_test"
        if request.method == "GET":
            return httpx2.Response(200 if self.created else 404, json={"message": "Not Found"})
        if request.url.path.endswith("/repos") and request.method == "POST":
            self.created = True
            return httpx2.Response(201, json={"full_name": "di-factory/client-x"})
        return httpx2.Response(202 if request.method == "POST" else 201, json={})


def _setup(examples: Path, tmp_path: Path, replies: list[str], **kw: Any) -> Setup:
    answers = iter(replies)
    return Setup(
        [examples], tmp_path / "clients", ask=lambda _: next(answers), ask_secret=lambda _: KEY,
        run=lambda argv: main(argv, provider=FakeProvider([Message.assistant("Hola.")])),
        public_url=URL, root=tmp_path / "repo", **kw,
    )  # fmt: skip


def test_every_setup_step_is_a_commit_in_the_clients_own_repository(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = ["dental appointments on WhatsApp", "1", *_replies(examples), "y"]
    assert _setup(examples, tmp_path, first).run_all() == 0
    folder = tmp_path / "clients" / ID
    assert client_repo.is_repo(folder)
    assert _log(folder) == [f"Signed for docker at {URL.removeprefix('https://')}",
                            "Setup: first build"]  # fmt: skip
    readme = (folder / "README.md").read_text()
    assert "Clínica Sonrisa" in readme and ID in readme and "@@" not in readme
    assert (folder / "HARNESS_VERSION").read_text().strip()
    assert (folder / ".github" / "workflows" / "validate.yml").exists()
    tracked = _git(folder, "ls-files").split()
    assert f"{ID}.json" in tracked and f"{ID}.docker.approval.json" in tracked
    assert f"{ID}.knowledge/clinic_faq.md" in tracked
    assert "client publish" in capsys.readouterr().out  # how to put it on GitHub

    again = ["", "1", "", "", "n"]  # key, client 1, rebuild, same look, not online now
    assert _setup(examples, tmp_path, again).run_all() == 0
    assert _log(folder)[0] == "Fine-tuning: rebuilt from its answers"


def test_with_a_token_it_is_published_privately_and_pushed_after_every_step(
    examples: Path, tmp_path: Path, home: Path
) -> None:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    api = FakeGitHub()
    setup = _setup(examples, tmp_path, ["dental appointments on WhatsApp", "1",
                                        *_replies(examples), "y"],
                   github=lambda: GitHub("ghp_test"))  # fmt: skip
    setup.repo_http = httpx2.Client(transport=httpx2.MockTransport(api))
    setup.repo_remote = str(bare)
    assert setup.run_all() == 0
    assert api.calls[:2] == [
        ("GET", f"/repos/di-factory/client-{ID}", None),
        ("POST", "/orgs/di-factory/repos",
         {"name": f"client-{ID}", "private": True,
          "description": f"Di-Factory client solution {ID}"}),
    ]  # fmt: skip
    assert len(api.calls) == 2  # the signature is pushed to the repository already there
    assert _git(bare, "log", "--format=%s", "main").splitlines() == _log(
        tmp_path / "clients" / ID)  # fmt: skip


def test_keys_and_secrets_never_enter_a_client_repository(tmp_path: Path) -> None:
    folder = client_repo.create(tmp_path / "c", "acme", name="Acme", packs=["p"])
    for name in ("jag.key", ".env", "secrets/anthropic", ".dif/online", "deploy/x"):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text("secret")
    (folder / "acme.json").write_text("{}")
    assert client_repo.commit(folder, "first")
    assert sorted(_git(folder, "ls-files").split()) == [
        ".github/workflows/validate.yml", ".gitignore", "HARNESS_VERSION", "README.md",
        "acme.json",
    ]  # fmt: skip
    assert not client_repo.commit(folder, "nothing changed")


def test_the_repository_is_handed_to_the_client() -> None:
    api = FakeGitHub()
    http = httpx2.Client(transport=httpx2.MockTransport(api))
    gh = GitHub("ghp_test", org="di-factory")
    said = client_repo.hand_to("acme", gh, to="roberta-plomeria", http=http)
    assert "roberta-plomeria accepts it" in said
    assert client_repo.hand_to("acme", gh, to="roberta", mode="invite", http=http)
    assert api.calls == [
        ("POST", "/repos/di-factory/client-acme/transfer", {"new_owner": "roberta-plomeria"}),
        ("PUT", "/repos/di-factory/client-acme/collaborators/roberta", {"permission": "push"}),
    ]

    refused = httpx2.Client(transport=httpx2.MockTransport(
        lambda _: httpx2.Response(403, json={"message": "Must have admin rights"})))  # fmt: skip
    with pytest.raises(client_repo.ClientRepoError, match="403 Must have admin rights"):
        client_repo.hand_to("acme", gh, to="x", http=refused)


def test_a_client_built_before_repositories_gets_its_own_folder(
    examples: Path, tmp_path: Path, home: Path
) -> None:
    first = ["dental appointments on WhatsApp", "1", *_replies(examples), "n"]
    assert _setup(examples, tmp_path, first).run_all() == 0
    clients, old = tmp_path / "clients", tmp_path / "harness-clients"
    old.mkdir()
    for item in sorted((clients / ID).iterdir()):  # the old, flat layout in the harness
        if item.name.startswith(ID):
            item.rename(old / item.name)
    reuse = _setup(examples, tmp_path, ["", "1", "n", "n"], legacy=old)
    assert reuse.run_all() == 0  # key, reuse, no rebuild, offline
    assert (clients / ID / f"{ID}.json").exists() and not (old / f"{ID}.json").exists()
    assert (clients / ID / f"{ID}.knowledge" / "clinic_faq.md").exists()
    assert _log(clients / ID)[0] == "Fine-tuning: checked again"


def test_the_handover_is_written_and_committed_in_the_repository(
    examples: Path, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = ["dental appointments on WhatsApp", "1", *_replies(examples), "y"]
    assert _setup(examples, tmp_path, first).run_all() == 0
    folder = tmp_path / "clients" / ID
    capsys.readouterr()
    main(["handover", str(folder / f"{ID}.json"), "--packs", str(examples),
          "--owner", "Roberta", "--url", URL])  # fmt: skip
    out = capsys.readouterr().out
    assert (folder / "CLAUDE.md").exists() and (folder / "docs" / "negocio.md").exists()
    assert _log(folder)[0] == "Handover to Roberta"
    assert "not on GitHub yet" in out and f"client publish {folder}" in out
    assert main(["client", "publish", str(folder)]) == 2  # no token stored
    assert "secrets set github" in capsys.readouterr().err
