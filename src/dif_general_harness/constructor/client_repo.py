"""Each client solution is its own git repository, created from ``templates/client``.

The setup builds a client in ``clients/<id>/``: a git repository made from the template (a
README of its layout, a ``.gitignore`` that keeps keys and deploy folders out, a CI check
that the instance validates against the harness it is pinned to, ``HARNESS_VERSION``).
Every build, fine-tuning round and signature is a commit, so the history is the record of
what changed. With a GitHub token (the ``github`` secret) the repository is published as a
private ``client-<id>`` in Di-Factory's organization and pushed after every commit; at the
handover it moves to the client (a transfer or an invitation).

The token is passed to git per command (an HTTP header), never written into the
repository's configuration, and the GitHub API is called with ``httpx2``.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from .deploy import REPO_ROOT

TEMPLATE = REPO_ROOT / "templates" / "client"
API = "https://api.github.com"
DEFAULT_ORG = "di-factory"
COMMITTER = ("-c", "user.name=Di-Factory setup", "-c", "user.email=setup@di-factory.local")


class ClientRepoError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHub:
    token: str
    org: str = DEFAULT_ORG
    api: str = API

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise ClientRepoError(f"git {args[0]}: {result.stderr.strip() or result.stdout.strip()}")
    return result


def harness_version(root: Path = REPO_ROOT) -> str:
    """The harness commit this machine runs (what a client is pinned to)."""
    found = _git(root, "rev-parse", "HEAD", check=False)
    return found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else "main"


def repo_name(instance_id: str) -> str:
    return f"client-{instance_id}"


def is_repo(folder: Path) -> bool:
    return (folder / ".git").exists()


def create(folder: Path, instance_id: str, *, name: str, packs: list[str],
           template: Path = TEMPLATE, harness: Path = REPO_ROOT) -> Path:  # fmt: skip
    """Make ``folder`` a client repository from the template (files already there stay)."""
    folder.mkdir(parents=True, exist_ok=True)
    marks = {"@@ID@@": instance_id, "@@NAME@@": name, "@@PACKS@@": ", ".join(packs)}
    for source in sorted(template.rglob("*")):
        if source.is_dir():
            continue
        target = folder / source.relative_to(template)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        text = source.read_text(encoding="utf-8")
        for mark, value in marks.items():
            text = text.replace(mark, value)
        target.write_text(text, encoding="utf-8")
    (folder / "HARNESS_VERSION").write_text(harness_version(harness) + "\n", encoding="utf-8")
    if not is_repo(folder):
        _git(folder, "init", "-q", "-b", "main")
    return folder


def commit(folder: Path, message: str) -> bool:
    """Commit everything (the .gitignore keeps secrets out); False when nothing changed."""
    _git(folder, "add", "-A")
    if not _git(folder, "status", "--porcelain").stdout.strip():
        return False
    _git(folder, *COMMITTER, "commit", "-q", "-m", message)
    return True


def _auth_header(token: str) -> str:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"http.extraHeader=Authorization: Basic {basic}"


def publish(folder: Path, instance_id: str, gh: GitHub, *,
            http: httpx2.Client | None = None, remote: str | None = None) -> str:  # fmt: skip
    """Create the private GitHub repository if it does not exist yet, and push to it.
    Returns its address. ``remote`` overrides the push URL (tests: a local bare repo)."""
    name = repo_name(instance_id)
    client = http or httpx2.Client(timeout=30.0)
    try:
        found = client.get(f"{gh.api}/repos/{gh.org}/{name}", headers=gh.headers)
        if found.status_code == 404:
            created = client.post(
                f"{gh.api}/orgs/{gh.org}/repos", headers=gh.headers,
                json={"name": name, "private": True,
                      "description": f"Di-Factory client solution {instance_id}"},
            )  # fmt: skip
            if created.status_code >= 400:
                raise ClientRepoError(f"GitHub refused to create {gh.org}/{name}:"
                                      f" {created.status_code} {_message(created)}")  # fmt: skip
        elif found.status_code >= 400:
            raise ClientRepoError(f"GitHub: {found.status_code} {_message(found)}")
    finally:
        if http is None:
            client.close()
    url = remote or f"https://github.com/{gh.org}/{name}.git"
    if _git(folder, "remote", "get-url", "origin", check=False).returncode != 0:
        _git(folder, "remote", "add", "origin", url)
    else:
        _git(folder, "remote", "set-url", "origin", url)
    extra = () if remote else ("-c", _auth_header(gh.token))
    _git(folder, *extra, "push", "-q", "-u", "origin", "main")
    return f"https://github.com/{gh.org}/{name}"


def push(folder: Path, gh: GitHub) -> bool:
    """Push new commits to an already published repository; False when not published."""
    if _git(folder, "remote", "get-url", "origin", check=False).returncode != 0:
        return False
    url = _git(folder, "remote", "get-url", "origin").stdout.strip()
    extra = ("-c", _auth_header(gh.token)) if url.startswith("https://") else ()
    _git(folder, *extra, "push", "-q", "origin", "main")
    return True


def hand_to(instance_id: str, gh: GitHub, *, to: str, mode: str = "transfer",
            http: httpx2.Client | None = None) -> str:  # fmt: skip
    """At the handover: transfer the repository to the client's account (they accept it
    in GitHub) or invite them with write access. Returns what happened, in one line."""
    name = repo_name(instance_id)
    client = http or httpx2.Client(timeout=30.0)
    try:
        if mode == "transfer":
            r = client.post(f"{gh.api}/repos/{gh.org}/{name}/transfer", headers=gh.headers,
                            json={"new_owner": to})  # fmt: skip
            done = f"transfer of {gh.org}/{name} to {to} requested: {to} accepts it in GitHub"
        else:
            r = client.put(f"{gh.api}/repos/{gh.org}/{name}/collaborators/{to}",
                           headers=gh.headers, json={"permission": "push"})  # fmt: skip
            done = f"{to} invited to {gh.org}/{name} with write access"
    finally:
        if http is None:
            client.close()
    if r.status_code >= 400:
        raise ClientRepoError(f"GitHub: {r.status_code} {_message(r)}")
    return done


def _message(r: httpx2.Response) -> str:
    try:
        data: Any = r.json()
    except ValueError:
        return r.text[:200]
    return str(data.get("message", "")) if isinstance(data, dict) else str(data)[:200]


def github_from_secrets() -> GitHub | None:
    """The GitHub connection the setup uses, if a token is stored."""
    from ..tenancy import local_backend

    token = local_backend().get("github")
    if not token:
        return None
    return GitHub(token=token, org=os.environ.get("DIF_GITHUB_ORG") or DEFAULT_ORG)


def has_remote(folder: Path) -> bool:
    return (
        is_repo(folder) and _git(folder, "remote", "get-url", "origin", check=False).returncode == 0
    )


def place(built: Path, folder: Path) -> None:
    """Move what a build wrote in a scratch folder into the client's folder, replacing the
    files of an earlier build (a client's FAQ folder is replaced whole)."""
    folder.mkdir(parents=True, exist_ok=True)
    for item in sorted(built.iterdir()):
        target = folder / item.name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        shutil.move(str(item), str(target))


def adopt(spec_path: Path, folder: Path) -> Path:
    """Move a client built before client repositories (``clients/<id>.json`` and its
    answers, summary, FAQ and approvals) into its own folder; returns the new spec path."""
    stem = spec_path.name.removesuffix(".json")
    folder.mkdir(parents=True, exist_ok=True)
    for item in sorted(spec_path.parent.glob(f"{stem}.*")):
        shutil.move(str(item), str(folder / item.name))
    return folder / spec_path.name
