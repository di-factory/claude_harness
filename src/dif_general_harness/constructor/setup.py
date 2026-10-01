"""Guided setup: from a fresh clone to a working (and optionally online) client solution.

``./setup.sh`` installs the tools and runs ``dif-general-harness setup``, which:

1. stores the model API key once (``~/.dif/secrets``), refusing tokens that cannot work;
2. asks what the client needs and picks the pack;
3. runs the pack's questionnaire, the client's business included, with Di-Factory's
   defaults filled in (models; anything Di-Factory does not know yet is marked ``pending``);
4. builds and validates the instance, and sends it one real test question;
5. on request, prepares it to go online: signs it (Jag's key, created on first use),
   stages the Docker deploy behind HTTPS, copies the secrets it needs and writes the proxy
   configuration. ``setup.sh`` then starts it with root rights (Docker, Caddy).

Everything it writes can be redone by hand with the individual commands, and every answer
is kept in ``<instance>.answers.yaml``, so a correction is "edit, run build again".
"""

from __future__ import annotations

import getpass
import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..spec.loader import PackCatalog, load_instance
from ..tenancy import FileSecrets, default_secrets_dir, local_backend
from .build import BuildResult, build
from .catalog import match
from .deploy import REPO_ROOT, approve, check_approval, new_key, plan_docker, stage
from .interview import Question

Ask = Callable[[str], str]
DIFACTORY_DEFAULTS = {"main_model": "claude-sonnet-5-5", "fast_model": "claude-haiku-4-5"}
TEST_QUESTION = {"es": "¿De qué se trata este negocio?", "en": "What is this business about?"}
ONLINE_MARKER = Path(".dif") / "online"


def _say(text: str = "") -> None:
    print(text, flush=True)


def _step(n: int, title: str) -> None:
    _say(f"\n── Step {n}/5 · {title} " + "─" * max(0, 50 - len(title)))


def _yes(answer: str, default: bool = False) -> bool:
    text = answer.strip().lower()
    return default if not text else text in {"y", "yes", "s", "si", "sí"}


class Setup:
    def __init__(
        self,
        packs: list[Path],
        out: Path,
        *,
        ask: Ask = input,
        ask_secret: Ask = getpass.getpass,
        run: Callable[[list[str]], int] | None = None,
        public_url: str | None = None,
        root: Path = REPO_ROOT,
    ) -> None:
        self.root = root  # where deploy/build and .dif live (the repository)
        self.catalog = PackCatalog(roots=packs)
        self.packs = packs
        self.out = out
        self.ask = ask
        self.ask_secret = ask_secret
        self.run = run  # the CLI's main, to send the test message
        self.public_url = public_url
        self.pending: list[str] = []

    # --- 1. the key ---------------------------------------------------------------------

    def model_key(self) -> bool:
        _step(1, "Model API key")
        found = local_backend().get("anthropic")
        if found:
            _say(f"Found an Anthropic key ({len(found)} characters, {found[:10]}...).")
            if not _yes(self.ask("Use it? [Y/n] "), default=True):
                found = None
        while not found:
            _say("Paste the Anthropic API key (console.anthropic.com → API Keys, inside a"
                 " workspace). Nothing is shown while you paste; press Enter after.")  # fmt: skip
            value = self.ask_secret("API key: ").strip()
            if not value:
                _say("Nothing arrived (some terminals hide pastes). Paste it here instead;"
                     " it will be visible on your screen only:")  # fmt: skip
                value = self.ask("API key: ").strip()
            if value.startswith("sk-ant-oat"):
                _say("That is a Claude subscription (OAuth) token: it cannot run applications."
                     " Create an API key in the Console.")  # fmt: skip
                continue
            if not value.startswith("sk-"):
                _say("That does not look like an API key (it starts with sk-). Try again.")
                continue
            folder = default_secrets_dir()
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o700)
            path = folder / "anthropic"
            path.write_text(value, encoding="utf-8")
            path.chmod(0o600)
            _say(f"Saved in {path} (only you can read it). It is reused on every login.")
            found = value
        return True

    # --- 2. the pack --------------------------------------------------------------------

    def choose_pack(self) -> str | None:
        _step(2, "What does the client need?")
        packs = {layer.data["solution"]["id"]: layer.data for layer in self.catalog.latest()}
        request = self.ask("In a sentence (e.g. 'WhatsApp appointments for a dental clinic'): ")
        found = [m.pack_id for m in match(self.catalog, request)] if request.strip() else []
        options = found or sorted(packs)
        if not found:
            _say("No pack matched those words; these are all the packs:")
        for i, pid in enumerate(options, 1):
            sol = packs[pid]["solution"]
            _say(f"  {i}. {sol.get('name') or pid}: {sol.get('description', '')[:90]}")
        choice = self.ask(f"Which one? [1-{len(options)}, Enter = 1] ").strip() or "1"
        if not choice.isdigit() or not 1 <= int(choice) <= len(options):
            _say("Not a valid choice.")
            return None
        return options[int(choice) - 1]

    # --- 3. the questionnaire -----------------------------------------------------------

    def _question(self, q: Question, error: str | None) -> str:
        if error:
            _say(f"  ! {error}")
        who = {"client": "the client", "difactory": "Di-Factory"}.get(q.answered_by, "")
        head = f"\n[{q.group}{' · ' + who if who else ''}] {q.text}"
        _say(head + ("  (required)" if q.required else ""))
        if q.example:
            _say(f"   e.g. {q.example}")
        later = q.answered_by == "difactory" and q.default is None and q.kind == "string"
        if q.default is not None:
            _say(f"   Enter keeps: {json.dumps(q.default, ensure_ascii=False)}")
        elif later:
            _say("   Enter leaves it pending: Di-Factory sets it later")
        answer = self.ask("> ")
        if not answer.strip() and later:
            self.pending.append(q.name)
            return "pending"  # Di-Factory fills it in later (adjust --set)
        return answer

    def questionnaire(self, pack_id: str) -> BuildResult:
        _step(3, "Questionnaire (the client's business, then a few settings)")
        _say("Answer in the client's language. Enter keeps a default; Di-Factory's own"
             " settings already have sensible defaults.")  # fmt: skip
        variables = self.catalog.find(pack_id).data.get("variables", {})
        defaults = {f"values.{k}": v for k, v in DIFACTORY_DEFAULTS.items() if k in variables}
        return build(self.catalog, [pack_id], self.out, answers=defaults, ask=self._question)

    # --- 4. try it ----------------------------------------------------------------------

    def try_it(self, result: BuildResult) -> None:
        _step(4, "Built; one real test question")
        assert result.resolved is not None
        spec = result.resolved.spec
        _say(f"Instance: {result.spec_path}  (summary: {result.summary_path})")
        locale = (spec.solution.locale or "en")[:2]
        question = TEST_QUESTION.get(locale, TEST_QUESTION["en"])
        _say(f"Asking: {question}\n")
        if self.run is not None:
            packs = [a for p in self.packs for a in ("--packs", str(p))]
            self.run(["run", str(result.spec_path), *packs, "-m", question])
        missing = [n for n in sorted(spec.secrets) if n != "anthropic"
                   and not local_backend().get(n)]  # fmt: skip
        if missing:
            _say(f"\nNot connected yet (those parts stay off): {', '.join(missing)}."
                 " Add each with: dif-general-harness secrets set NAME")  # fmt: skip
        if self.pending:
            _say(f"Pending Di-Factory settings: {', '.join(self.pending)}. Set them with:"
                 f" dif-general-harness adjust {result.spec_path} --set NAME=VALUE")  # fmt: skip

    # --- 5. online ----------------------------------------------------------------------

    def go_online(self, result: BuildResult) -> Path | None:
        _step(5, "Put it online")
        if not self.public_url:
            _say("No public address known (run ./setup.sh on the server). Skipping.")
            return None
        host = self.public_url.removeprefix("https://").rstrip("/")
        if not _yes(self.ask(f"Serve it at {self.public_url} now? [y/N] ")):
            _say("Not now. Run ./setup.sh again whenever you want it online.")
            return None
        instance = result.spec_path
        keys = Path.home() / ".dif" / "keys"
        key = keys / "jag.key"
        approvers_path = self.root / ".dif" / "approvers.json"
        approvers = (
            json.loads(approvers_path.read_text(encoding="utf-8"))
            if approvers_path.exists() else {}
        )  # fmt: skip
        if not key.exists():
            _, public = new_key(keys, "jag")
            approvers["jag"] = public
            _say(f"Created the approver key {key} (keep a backup; it signs every deploy).")
        elif "jag" not in approvers:
            _say(f"{approvers_path} does not trust {key}; add its public key and run again.")
            return None
        approvers_path.parent.mkdir(parents=True, exist_ok=True)
        approvers_path.write_text(json.dumps(approvers, indent=2) + "\n", encoding="utf-8")
        _say("Jag signs exactly this solution (any later change needs a new signature).")
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            staged = stage(instance, self.catalog, Path(tmp) / "solution")
            record = approve(staged, Path(tmp) / "solution", "docker", key, "jag")
        approval = instance.with_suffix(".docker.approval.json")
        approval.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        staged_id = load_instance(instance, self.catalog).spec.solution.id
        folder = self.root / "deploy" / "build" / staged_id
        folder.mkdir(parents=True, exist_ok=True)
        staged = stage(instance, self.catalog, folder / "solution")
        check_approval(record, folder / "solution", "docker", approvers)
        plan = plan_docker(staged, folder, public_url=self.public_url)
        store = folder / "secrets"
        mine = local_backend()
        files = FileSecrets(default_secrets_dir())
        if not (mine.get("admin_token") or files.get("admin_token")):
            token = secrets.token_hex(24)
            target = default_secrets_dir() / "admin_token"
            target.write_text(token, encoding="utf-8")
            target.chmod(0o600)
        for name in sorted(plan.secrets):
            value = mine.get(name) or files.get(name)
            if value:
                (store / name).write_text(value, encoding="utf-8")
                (store / name).chmod(0o644)  # read by the container's own user
        (folder / "Caddyfile").write_text(
            f"{host} {{\n    reverse_proxy 127.0.0.1:8080\n}}\n", encoding="utf-8"
        )
        marker = self.root / ONLINE_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{folder}\n{host}\n", encoding="utf-8")
        _say(f"Staged and signed in {folder}. setup.sh now starts it (Docker and Caddy).")
        return folder

    # --- all ----------------------------------------------------------------------------

    def existing(self) -> BuildResult | None:
        """A client built before: reuse it instead of answering everything again."""
        found = []
        for path in sorted(self.out.glob("*.json")) if self.out.is_dir() else []:
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("kind") == "instance":
                    found.append(path)
            except (OSError, ValueError):
                continue
        if not found:
            return None
        _say("\nClients already set up here:")
        for i, path in enumerate(found, 1):
            _say(f"  {i}. {path.stem}")
        choice = self.ask(f"Reuse one? [1-{len(found)}, Enter = new client] ").strip()
        if not choice.isdigit() or not 1 <= int(choice) <= len(found):
            return None
        path = found[int(choice) - 1]
        resolved = load_instance(path, self.catalog)
        return BuildResult(
            path.stem, path, path.with_suffix(".answers.yaml"), path.with_suffix(".summary.md"),
            resolved,
        )  # fmt: skip

    def run_all(self) -> int:
        _say("Di-Factory harness setup. Ctrl+C stops at any time; nothing is half-written.")
        self.model_key()
        reused = self.existing()
        if reused is not None:
            self.try_it(reused)
            self.go_online(reused)
            return 0
        pack_id = self.choose_pack()
        if pack_id is None:
            return 2
        result = self.questionnaire(pack_id)
        if not result.ok:
            _say("\nNot ready yet:")
            for problem in result.problems:
                _say(f"  - {problem}")
            for issue in result.resolved.issues if result.resolved else []:
                if issue.severity == "error":
                    _say(f"  - {issue}")
            _say(f"Edit {result.answers_path} and run: dif-general-harness build --pack"
                 f" {pack_id} --answers {result.answers_path} --out {self.out}")  # fmt: skip
            return 1
        self.try_it(result)
        self.go_online(result)
        _say("\nDone. Chat with it any time:")
        packs = " ".join(f"--packs {p}" for p in self.packs)
        _say(f"  uv run dif-general-harness console {result.spec_path} {packs}")
        return 0


def default_packs() -> list[Path]:
    return [REPO_ROOT / "docs" / "spec" / "examples"]


def run_setup(args: Any, run: Callable[[list[str]], int] | None = None) -> int:
    setup = Setup(args.packs or default_packs(), args.out, run=run, public_url=args.public_url)
    try:
        return setup.run_all()
    except (KeyboardInterrupt, EOFError):
        _say("\nStopped. Run it again whenever you like.")
        return 130
