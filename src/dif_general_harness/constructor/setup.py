"""Guided setup: from a fresh clone to a working (and optionally online) client solution.

``./setup.sh`` installs the tools and runs ``dif-general-harness setup``, which:

1. stores the model API key once (``~/.dif/secrets``), refusing tokens that cannot work;
2. asks what the client needs; a small model recommends the pack, says what it covers and
   names the needs no pack covers (those go to the client's summary as Di-Factory design
   work); without the model it falls back to word matching;
3. runs the pack's questionnaire, the client's business included, with Di-Factory's
   defaults filled in (models; anything Di-Factory does not know yet is marked ``pending``);
   on request a business consultant (a top model) asks one follow-up when a business answer
   is thin and, at the end, recommends what to define or add (kept in the summary);
4. builds and validates the instance, and sends it one real test question;
5. on request, prepares it to go online: signs it (Jag's key, created on first use),
   stages the Docker deploy behind HTTPS, copies the secrets it needs and writes the proxy
   configuration. ``setup.sh`` then starts it with root rights (Docker, Caddy).

Everything it writes can be redone by hand with the individual commands, and every answer
is kept in ``<instance>.answers.yaml``, so a correction is "edit, run build again".
"""

from __future__ import annotations

import asyncio
import getpass
import json
import re
import secrets
import shlex
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.messages import Message
from ..knowledge.sources import SITE_PAGES
from ..providers.base import ModelProvider, ModelRequest, ProviderMessage
from ..runtime.routing import RoutingError, check_anthropic_key
from ..spec.errors import SpecError
from ..spec.loader import PackCatalog, load_instance
from ..tenancy import FileSecrets, default_secrets_dir, local_backend
from .brand import read_brand
from .build import BuildResult, _extends, build, pack_questions
from .catalog import match
from .deploy import REPO_ROOT, approve, check_approval, new_key, plan_docker, stage
from .impact import missing, report
from .interview import AnswerError, Question, load_answers, parse
from .site_reader import (
    WRITER_SYSTEM,
    file_for,
    pages_prompt,
    raw_markdown,
    read_site,
    site_entries,
    start_of,
    whole_site,
)

Ask = Callable[[str], str]
DIFACTORY_DEFAULTS = {"main_model": "claude-sonnet-5-5", "fast_model": "claude-haiku-4-5"}
TEST_QUESTION = {"es": "¿De qué se trata este negocio?", "en": "What is this business about?"}
ONLINE_MARKER = Path(".dif") / "online"
ADVISOR_MODEL = "claude-haiku-4-5"
WEB_CODE = "web_access_code"  # the secret a closed web chat asks for
CONSULTANT_MODEL = "claude-opus-5"  # DIF_CONSULTANT_MODEL changes it
# what a draft instance lacks only because nobody answered yet; anything else is a conflict
_UNANSWERED = {"missing_value", "invalid_value", "model_not_set", "unresolved_variable",
               "missing_file"}  # fmt: skip
Advisor = Callable[[str], ModelProvider]  # the API key -> a model to ask


@dataclass
class Advice:
    packs: list[str]
    covered: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    why: str = ""


_ADVISOR_SYSTEM = """You help Di-Factory pick a solution pack for a client.
Each pack is one ready-made product. One client instance runs ONE pack; recommend a second
pack only when the client clearly needs it too (it becomes a second instance).
Never claim a pack does something its description does not say.
Answer with JSON only:
{"packs": ["<pack id>", ...], "covered": ["<need the packs cover>", ...],
 "gaps": ["<need no pack covers>", ...], "why": "<one sentence>"}
"packs" may be empty when nothing fits. Write covered, gaps and why in the request's language."""


_GAPS_SYSTEM = """The client chose these solution packs. Name the client's needs that these
packs do not cover, judging only from the packs' descriptions; a need a chosen pack covers is
not a gap. Write them in the request's language.
Answer with JSON only: {"gaps": ["...", "..."]}"""

_COLORS_SYSTEM = """Turn a description of a brand's colors into a palette for its web page.
Keep every color the person named, in their order, and add only what they ask for (for
example "another blue to complete"). The first color is the main one: buttons and headings
use it, so it is never white, near-white or black (move those later). The second is an
accent. Give 2 to 5 colors.
Answer with JSON only: {"colors": ["#rrggbb", "#rrggbb", ...]}"""
_HEX = re.compile(r"#[0-9a-fA-F]{6}")

_CONSULTANT_FOLLOW_UP = """You are a senior business consultant helping a small business set up
the assistant that will answer its customers. The business answers a questionnaire; its
answers become the assistant's FAQ, and the assistant never says anything the FAQ does not.
Given one answer, decide whether a customer-facing assistant could use it as it is. If it is
thin or leaves out what customers will surely ask (prices, durations, conditions, what is
and is not offered, how to pay, what happens in an emergency...), write ONE short, concrete
follow-up question, in the language of the answer. Otherwise none.
Answer with JSON only: {"follow_up": "<question>"} or {"follow_up": null}"""

_CONSULTANT_REVIEW = """You are a senior business consultant. A small business has answered a
questionnaire that becomes the FAQ of its customer-facing assistant, which never invents
anything. Read the answers and give at most 6 concrete, prioritized recommendations: what
customers will ask that the answers do not cover, policies the business should define
(cancellations, payments, privacy, emergencies), and risks for this kind of business. Be
brief and specific to this business; do not repeat the uncovered needs already noted. Write
in the language of the answers.
Answer with JSON only: {"recommendations": ["...", "..."]}"""


def _json_object(text: str) -> dict[str, Any] | None:
    found = re.search(r"\{.*\}", text, re.S)
    if not found:
        return None
    try:
        raw = json.loads(found.group(0))
    except ValueError:
        return None
    return raw if isinstance(raw, dict) else None


def _pack_card(data: dict[str, Any]) -> str:
    sol = data["solution"]
    agents = "; ".join(
        f"{name}: {a.get('description') or ''}" for name, a in data.get("agents", {}).items()
    )
    needs = _needs(data)
    warning = f" (Its models: {needs}.)" if needs else ""
    return (f"- {sol['id']} ({sol.get('name') or sol['id']}): {sol.get('description') or ''}"
            f" Agents: {agents}{warning}")  # fmt: skip


def parse_advice(text: str, known: set[str]) -> Advice | None:
    """The advisor's JSON, keeping only packs that exist; None when it is not usable."""
    raw = _json_object(text)
    if raw is None:
        return None

    def strings(key: str) -> list[str]:
        value = raw.get(key)
        return [str(v) for v in value if str(v).strip()] if isinstance(value, list) else []

    packs = [p for p in dict.fromkeys(strings("packs")) if p in known]
    return Advice(packs, strings("covered"), strings("gaps"), str(raw.get("why") or ""))


def parse_choice(text: str, count: int) -> list[int] | None:
    """'1', '1,2', '1 and 2', '1 y 2' -> [0, 1]; None when a number is out of range."""
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    if not numbers or any(not 1 <= n <= count for n in numbers):
        return None
    return [n - 1 for n in dict.fromkeys(numbers)]


def _say(text: str = "") -> None:
    print(text, flush=True)


def _step(n: int, title: str) -> None:
    _say(f"\n── Step {n}/5 · {title} " + "─" * max(0, 50 - len(title)))


def _needs(data: dict[str, Any]) -> str:
    """What a pack's models need beyond the Anthropic key the setup stores, if anything."""
    providers = (data.get("models") or {}).get("providers") or {}
    other = sorted(name for name in providers if name != "anthropic")
    if not other:
        return ""
    return ", ".join(f"its own {name} model endpoint" for name in other) + "; can run on Anthropic"


def on_anthropic(data: dict[str, Any]) -> dict[str, Any]:
    """The instance layer that runs a pack's models on the Anthropic key instead of their
    own endpoint: every role on another provider moves to Anthropic (the models are
    Di-Factory's defaults), the other providers are dropped, and the key is declared."""
    models = data.get("models") or {}
    roles = {name: {"provider": "anthropic"} for name, role in (models.get("roles") or {}).items()
             if (role or {}).get("provider") != "anthropic"}  # fmt: skip
    providers: dict[str, Any] = {
        name: None for name in (models.get("providers") or {}) if name != "anthropic"
    }
    providers["anthropic"] = {"api_key": {"$secret": "anthropic"}}
    return {
        "models": {"roles": roles, "providers": providers},
        "secrets": {"anthropic": {"description": "Model API key"}},
    }


def _key_problem(key: str) -> str | None:
    try:
        check_anthropic_key(key, {})
    except RoutingError as exc:
        return str(exc)
    return None


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
        advisor: Advisor | None = None,
        consultant: Advisor | None = None,
        writer: Advisor | None = None,
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
        self.advisor = advisor  # None: word matching only (tests, or no key)
        self.consultant = consultant  # the business consultant (questionnaire help)
        self.consulting = False
        self.writer = writer  # writes a site's knowledge file from its pages
        self.site_http: Any = None  # tests: the transport the site is read with
        self.key: str | None = None
        self.gaps: list[str] = []
        self.request = ""
        self.business: dict[str, str] = {}  # the business answers so far, for the consultant
        self.recommendations: list[str] = []
        self.carried: dict[str, Any] = {}  # per-client choices kept in the answers (models)

    # --- 1. the key ---------------------------------------------------------------------

    def model_key(self) -> bool:
        _step(1, "Model API key")
        found = local_backend().get("anthropic")
        if found and (problem := _key_problem(found)):
            _say(f"The stored Anthropic key cannot work: {problem}.")
            found = None
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
            if problem := _key_problem(value):
                _say(f"That cannot work: {problem}. Paste it once more, just once.")
                continue
            folder = default_secrets_dir()
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o700)
            path = folder / "anthropic"
            path.write_text(value, encoding="utf-8")
            path.chmod(0o600)
            _say(f"Saved in {path}: {len(value)} characters, starts with {value[:10]}"
                 " (only you can read it). It is reused on every login.")  # fmt: skip
            found = value
        self.key = found
        return True

    # --- 2. the pack --------------------------------------------------------------------

    def _ask_model(
        self,
        factory: Advisor | None,
        who: str,
        system: str,
        user: str,
        *,
        role: str,
        max_tokens: int,
        timeout: float = 120,
    ) -> str | None:
        """One model call; None (and a short note) when it cannot answer."""
        if factory is None or not self.key:
            return None
        request = ModelRequest(system=system, messages=[Message.user(user)],
                               model_role=role, max_tokens=max_tokens)  # fmt: skip

        async def call() -> str:
            final = ""
            async for event in factory(self.key or "").stream(request):
                if isinstance(event, ProviderMessage):
                    final = event.message.text()
            return final

        try:
            return asyncio.run(asyncio.wait_for(call(), timeout=timeout))
        except Exception as exc:  # the setup goes on without it
            _say(f"(The {who} did not answer: {type(exc).__name__}. Going on without it.)")
            return None

    def advise(self, request: str, packs: dict[str, dict[str, Any]]) -> Advice | None:
        """Ask the small model which packs fit; None when it cannot answer."""
        if not request.strip():
            return None
        cards = "\n".join(_pack_card(packs[pid]) for pid in sorted(packs))
        text = self._ask_model(
            self.advisor, "advisor model", _ADVISOR_SYSTEM,
            f"Packs:\n{cards}\n\nThe client's need: {request}", role="fast", max_tokens=1024,
        )  # fmt: skip
        return parse_advice(text, set(packs)) if text is not None else None

    def gaps_for(self, chosen: list[str], packs: dict[str, dict[str, Any]]) -> list[str]:
        """The advisor's gaps were for the packs it recommended; another choice covers other
        needs. Ask again for the chosen packs; without an answer, record no gaps rather than
        wrong ones."""
        cards = "\n".join(_pack_card(packs[pid]) for pid in chosen)
        text = self._ask_model(
            self.advisor, "advisor model", _GAPS_SYSTEM,
            f"Chosen packs:\n{cards}\n\nThe client's need: {self.request}",
            role="fast", max_tokens=600,
        )  # fmt: skip
        raw = _json_object(text or "") or {}
        gaps = raw.get("gaps")
        found = [str(g) for g in gaps if str(g).strip()] if isinstance(gaps, list) else []
        if found:
            _say("With that choice, not covered yet: " + "; ".join(found))
        return found

    def _model_choice(self, chosen: list[str], packs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """A pack on another provider's endpoint can run on the Anthropic key instead."""
        for pid in chosen:
            if not _needs(packs[pid]):
                continue
            question = (f"{pid} runs on its own model endpoint (another provider's key). Run"
                        " it on the Anthropic key you gave instead? [Y/n] ")  # fmt: skip
            if _yes(self.ask(question), default=True):
                return on_anthropic(packs[pid])
            _say("   It will not answer until that endpoint's key is set (dif-general-harness"
                 " secrets set llm), so it is not put online.")  # fmt: skip
        return {}

    def conflicts(self, pack_ids: list[str]) -> list[str]:
        """Why these packs cannot run together in one instance (empty: they can)."""
        if len(pack_ids) < 2:
            return []
        first = self.catalog.find(pack_ids[0]).data["solution"]
        draft = {
            "spec_version": "1", "kind": "instance",
            "solution": {"id": "draft", "version": "1.0.0", "lob": first["lob"]},
            "extends": [_extends(self.catalog, p) for p in pack_ids],
            "tenant": {"id": "draft", "name": "draft"}, "values": {},
        }  # fmt: skip
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "draft.json"
            path.write_text(json.dumps(draft), encoding="utf-8")
            try:
                issues = load_instance(path, self.catalog).issues
            except SpecError as exc:
                issues = exc.issues
        found = [i for i in issues if i.severity == "error" and i.code not in _UNANSWERED]
        return list(dict.fromkeys(f"{i.code}: {i.message}" for i in found))

    def choose_pack(self) -> list[str] | None:
        _step(2, "What does the client need?")
        packs = {layer.data["solution"]["id"]: layer.data for layer in self.catalog.latest()}
        request = self.ask("In a sentence (e.g. 'WhatsApp appointments for a dental clinic'): ")
        self.request = request.strip()
        advice = self.advise(request, packs)
        if advice is None:
            found = [m.pack_id for m in match(self.catalog, request)] if request.strip() else []
            advice = Advice(found[:1])
        else:
            if advice.why:
                _say(f"\n{advice.why}")
            if advice.covered:
                _say("Covered: " + "; ".join(advice.covered))
            if advice.gaps:
                _say("Not covered by any pack yet (noted as Di-Factory design work): "
                     + "; ".join(advice.gaps))  # fmt: skip
        self.gaps = advice.gaps
        options = advice.packs + sorted(p for p in packs if p not in advice.packs)
        _say("\nPacks (★ recommended). One instance runs one pack; another need can be a"
             " second client instance later:")  # fmt: skip
        for i, pid in enumerate(options, 1):
            sol = packs[pid]["solution"]
            star = "★" if pid in advice.packs else " "
            needs = _needs(packs[pid])
            note = f" [needs: {needs}]" if needs else ""
            _say(
                f" {star}{i}. {sol.get('name') or pid}: {(sol.get('description') or '')[:90]}"
                + note
            )
        default = "1"  # the first recommendation (or the first pack)
        while True:
            answer = self.ask(f"Which one? [1-{len(options)}, Enter = {default}] ")
            picked = parse_choice(answer.strip() or default, len(options))
            if picked is None:
                _say(f"Type a number from 1 to {len(options)} (or several: 1,2).")
                continue
            chosen = [options[i] for i in picked]
            problems = self.conflicts(chosen)
            if not problems:
                if advice.packs and set(chosen) != set(advice.packs):
                    self.gaps = self.gaps_for(chosen, packs)
                self.carried = self._model_choice(chosen, packs)
                return chosen
            _say("These packs cannot run together in one instance yet:")
            for problem in problems[:5]:
                _say(f"  - {problem}")
            _say("Choose one now; the other can be a second client instance (run setup again).")

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
        if q.key.startswith("knowledge.") and answer.strip() and error is None:
            answer = self._follow_up(q, answer)
            self.business[q.heading or q.text] = answer
        return answer

    def _follow_up(self, q: Question, answer: str) -> str:
        """The consultant asks at most one question that makes a thin answer useful."""
        if not self.consulting:
            return answer
        so_far = "\n".join(f"- {k}: {v}" for k, v in self.business.items()) or "(none yet)"
        text = self._ask_model(
            self.consultant, "consultant", _CONSULTANT_FOLLOW_UP,
            f"The client's need: {self.request or '(not given)'}\n"
            f"Answers so far:\n{so_far}\n\nQuestion: {q.text}\nAnswer: {answer}",
            role="consultant", max_tokens=600,
        )  # fmt: skip
        raw = _json_object(text or "")
        follow = str(raw.get("follow_up") or "").strip() if raw else ""
        if not follow:
            return answer
        _say(f"   Consultant: {follow}")
        more = self.ask("> ").strip()
        return f"{answer.strip()}\n{more}" if more else answer

    def ask_brand(self, keeps: bool = False) -> dict[str, Any] | None:
        """The client's look, from whatever they have: files, folders or typed colors."""
        _say("\nBrand look (optional), for the landing page and the chat: a logo, a palette,"
             " a brand guide (PDF, slides, Word), a CSS theme, photos, a folder with any of"
             " these, colors like #1f5f4a #e8a33d, or in words (navy and gold, azul marino y"
             " blanco, dark blue plus another blue). Several: separate with spaces.")  # fmt: skip
        answer = self.ask("Enter keeps the current look: " if keeps else "Enter skips: ")
        if not answer.strip():
            return None
        try:
            items = shlex.split(answer)
        except ValueError:
            items = answer.split()
        describe = self._describe_colors if self.advisor is not None and self.key else None
        brand = read_brand(items, text=answer, describe=describe)
        for note in brand.notes:
            _say(f"   {note}")
        branding = brand.branding()
        if not branding:
            _say("   No usable colors or logo found there; the look stays as it is.")
            return None
        colors = branding.get("colors") or {}
        shown = ", ".join(f"{k} {v}" for k, v in colors.items()) or "default colors"
        _say(f"   Look: {shown}; logo: {'yes' if branding.get('logo') else 'none'}.")
        return branding

    def _describe_colors(self, text: str) -> list[str] | None:
        """A description of colors ("dark blue, light blue, and another blue") as a palette."""
        answer = self._ask_model(self.advisor, "advisor model", _COLORS_SYSTEM, text,
                                 role="fast", max_tokens=300)  # fmt: skip
        raw = _json_object(answer or "") or {}
        colors = raw.get("colors")
        if not isinstance(colors, list):
            return None
        valid = [c.lower() for c in colors if isinstance(c, str) and _HEX.fullmatch(c)]
        return list(dict.fromkeys(valid)) or None

    def questionnaire(self, pack_ids: list[str]) -> BuildResult:
        _step(3, "Questionnaire (the client's business, then a few settings)")
        _say("Answer in the client's language. Enter keeps a default; Di-Factory's own"
             " settings already have sensible defaults.")  # fmt: skip
        if self.consultant is not None and self.key:
            _say("A business consultant (a top model) can help: one follow-up when an answer"
                 " is thin, and recommendations at the end. Costs a few cents.")  # fmt: skip
            self.consulting = _yes(self.ask("Use the consultant? [Y/n] "), default=True)
        variables = {k for p in pack_ids for k in self.catalog.find(p).data.get("variables", {})}
        defaults: dict[str, Any] = {
            f"values.{k}": v for k, v in DIFACTORY_DEFAULTS.items() if k in variables
        }
        branding = self.ask_brand()
        if branding:
            defaults["branding"] = branding
        self.ask_web_access(pack_ids)
        defaults.update(self.carried)
        result = build(self.catalog, pack_ids, self.out, answers=defaults, ask=self._question)
        if self.gaps:
            with result.summary_path.open("a", encoding="utf-8") as summary:
                summary.write("\n## Needs not covered yet (Di-Factory design work)\n\n")
                summary.writelines(f"- {gap}\n" for gap in self.gaps)
        self.review()
        if self.recommendations:
            with result.summary_path.open("a", encoding="utf-8") as summary:
                summary.write("\n## The business consultant's recommendations\n\n")
                summary.writelines(f"- {r}\n" for r in self.recommendations)
        return result

    def ask_web_access(self, pack_ids: list[str]) -> None:
        """A pack's open web chat: keep it open to anyone with the link, or close it for
        this client behind an access code (closing is a tightening, so the instance may)."""
        names = [
            name for p in pack_ids
            for name, ch in (self.catalog.find(p).data.get("channels") or {}).items()
            if isinstance(ch, dict) and ch.get("type") == "web" and ch.get("public") is True
        ]  # fmt: skip
        if not names or _yes(self.ask(
            "Should anyone with the link use the web chat (and see the web page's chat"
            " buttons)? It answers from what this client gives it. [Y/n] "
        ), default=True):  # fmt: skip
            return
        channels = self.carried.setdefault("channels", {})
        for name in names:
            channels[name] = {"public": False, "credentials": {"$secret": WEB_CODE}}
        self.carried.setdefault("secrets", {})[WEB_CODE] = {
            "description": "The web chat's access code (for the people allowed to chat)"
        }
        if not (local_backend().get(WEB_CODE) or FileSecrets(default_secrets_dir()).get(WEB_CODE)):
            target = default_secrets_dir() / WEB_CODE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(secrets.token_urlsafe(9), encoding="utf-8")
            target.chmod(0o600)
        _say(f"   The chat asks for an access code: it is in {default_secrets_dir() / WEB_CODE}"
             " (give it only to the people who should chat; the page shows them no chat"
             " buttons).")  # fmt: skip

    def review(self) -> None:
        """The consultant reads all the business answers and says what to improve."""
        if not self.consulting or not self.business:
            return
        answers = "\n".join(f"## {k}\n{v}" for k, v in self.business.items())
        text = self._ask_model(
            self.consultant, "consultant", _CONSULTANT_REVIEW,
            f"The client's need: {self.request or '(not given)'}\n"
            f"Uncovered needs already noted: {'; '.join(self.gaps) or 'none'}\n\n{answers}",
            role="consultant", max_tokens=1500,
        )  # fmt: skip
        raw = _json_object(text or "")
        items = raw.get("recommendations") if raw else None
        self.recommendations = [str(i) for i in items if str(i).strip()][:8] if isinstance(
            items, list) else []  # fmt: skip
        if self.recommendations:
            _say("\nThe consultant recommends (also in the summary):")
            for item in self.recommendations:
                _say(f"  - {item}")

    # --- 4. try it ----------------------------------------------------------------------

    def try_it(self, result: BuildResult) -> bool:
        _step(4, "Built; one real test question")
        assert result.resolved is not None
        spec = result.resolved.spec
        _say(f"Instance: {result.spec_path}  (summary: {result.summary_path})")
        locale = (spec.solution.locale or "en")[:2]
        question = TEST_QUESTION.get(locale, TEST_QUESTION["en"])
        _say(f"Asking: {question}\n")
        worked = True
        if self.run is not None:
            packs = [a for p in self.packs for a in ("--packs", str(p))]
            worked = self.run(["run", str(result.spec_path), *packs, "-m", question]) == 0
        store = local_backend()
        items = missing(
            spec, result.resolved.data, lambda n: n == "anthropic" or bool(store.get(n)),
            self.pending,
        )  # fmt: skip
        if items:
            _say("\nNot connected yet. The agent works without these; this is what stays off:")
            for item in items:
                _say("\n".join(f"  {line}" for line in item.lines()).replace(
                    "INSTANCE.json", str(result.spec_path)))  # fmt: skip
            with result.summary_path.open("a", encoding="utf-8") as summary:
                summary.write("\n" + report(items).replace("INSTANCE.json", str(result.spec_path)))
        if not worked:
            _say("\nThe test question did not work (see the message above), so it is not put"
                 " online: it would not start there either. Fix that and run ./setup.sh"
                 " again (reuse this client).")  # fmt: skip
        return worked

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
        try:
            staged_folder = self._stage_files(staged, folder, host)
        except PermissionError as exc:
            _say(f"Cannot write {exc.filename}: it belongs to the container's user from an"
                 f" earlier run. Fix: sudo chown -R $USER {folder / 'secrets'}  then run"
                 " ./setup.sh again.")  # fmt: skip
            return None
        return staged_folder

    def _stage_files(self, staged: Any, folder: Path, host: str) -> Path:
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

    def refresh(self, reused: BuildResult) -> BuildResult:
        """Rebuild a reused client from its saved answers, so pack updates (wording, new
        settings, fixes) reach it. Nothing changes unless the rebuild validates."""
        if not reused.answers_path.exists():
            return reused
        if not _yes(self.ask("Rebuild it from its saved answers? It brings in pack updates;"
                             " hand edits to its FAQ files are replaced (edit the answers"
                             " file instead). [Y/n] "), default=True):  # fmt: skip
            return reused
        try:
            answers = load_answers(reused.answers_path)
            extends = json.loads(reused.spec_path.read_text(encoding="utf-8"))["extends"]
            packs = [str(ref).split("@", 1)[0] for ref in extends]
            questions = pack_questions(self.catalog, packs)
        except Exception as exc:  # a broken answers file: keep the client as it is
            _say(f"Could not rebuild ({exc}); keeping it as it is.")
            return reused
        branding = self.ask_brand(keeps=bool(answers.get("branding")))
        if branding:
            answers["branding"] = branding
        self.ask_new_questions(questions, answers)
        try:
            with tempfile.TemporaryDirectory() as tmp:  # a dry run first: nothing half-written
                trial = build(self.catalog, packs, Path(tmp), answers=answers)
        except Exception as exc:  # a broken answers file: keep the client as it is
            _say(f"Could not rebuild ({exc}); keeping it as it is.")
            return reused
        if not trial.ok:
            _say("Its saved answers no longer pass with the current pack; keeping it as it is:")
            for problem in trial.problems:
                _say(f"  - {problem}")
            for issue in trial.resolved.issues if trial.resolved else []:
                if issue.severity == "error":
                    _say(f"  - {issue}")
            return reused
        result = build(self.catalog, packs, self.out, answers=answers)
        _say(f"Rebuilt {result.instance_id} from {reused.answers_path.name}.")
        return result

    def ask_new_questions(self, questions: list[Question], answers: dict[str, Any]) -> None:
        """Required questions the pack added since this client was built: asked now, so the
        rebuild does not fail on them (and the pack's example content never stays in use)."""
        new = [q for q in questions if q.required and q.default is None
               and q.key not in answers and q.name not in answers]  # fmt: skip
        if not new:
            return
        _say(f"\nThe pack asks {len(new)} new question(s) since this client was built:")
        for q in new:
            error = None
            while True:
                raw = self._question(q, error)
                try:
                    parse(q, raw)
                except AnswerError as exc:
                    error = str(exc)
                    continue
                answers[q.key] = raw
                break

    def read_sites(self, result: BuildResult) -> BuildResult:
        """Web addresses among the documents: read them now (a site's home: every page) and
        have a model write each up as a knowledge file, part of the signed solution, in
        place of the live address."""
        if not result.ok:
            return result
        spec = json.loads(result.spec_path.read_text(encoding="utf-8"))
        values = spec.get("values") or {}
        sites = site_entries(values)
        if not sites:
            return result
        folder = result.spec_path.parent
        changed = False
        for url in sites:
            rel = f"{result.instance_id}.knowledge/{file_for(url)}"
            target = folder / rel
            if target.exists():
                question = f"Read {url} again (it may have changed)? [y/N] "
                again = _yes(self.ask(question))
            else:
                what = "the whole site" if whole_site(url) else "the page"
                question = (f"Read {what} {url} now and have a model write the knowledge"
                            " file from it? [Y/n] ")  # fmt: skip
                again = _yes(self.ask(question), default=True)
            if again and not self._write_site(url, target, spec):
                again = False
            if target.exists():
                for value in values.values():
                    if isinstance(value, list) and url in value:
                        value[value.index(url)] = rel
                changed = True
            elif not again:
                _say(f"   {url} stays a live source: the running assistant reads it itself.")
        if not changed:
            return result
        result.spec_path.write_text(
            json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        resolved = load_instance(result.spec_path, self.catalog)
        return BuildResult(result.instance_id, result.spec_path, result.answers_path,
                           result.summary_path, resolved, result.problems)  # fmt: skip

    def _write_site(self, url: str, target: Path, spec: dict[str, Any]) -> bool:
        _say(f"   Reading {url} ...")
        try:
            limit = SITE_PAGES if whole_site(url) else 1
            pages = asyncio.run(read_site(start_of(url), max_pages=limit, http=self.site_http))
        except Exception as exc:  # the site is down or refuses: keep what there was
            _say(f"   Could not read it ({type(exc).__name__}: {exc}).")
            return False
        if not pages:
            _say("   No pages found there.")
            return False
        _say(f"   {len(pages)} page(s) read; the writer model turns them into questions and"
             " answers (a minute or two).")  # fmt: skip
        name = str((spec.get("tenant") or {}).get("name") or url)
        text = self._ask_model(self.writer, "writer model", WRITER_SYSTEM,
                               f"Business: {name}\n\n{pages_prompt(pages)}",
                               role="main", max_tokens=16000, timeout=600)  # fmt: skip
        if not text or "## " not in text:
            text = raw_markdown(pages, name)  # no model: the pages as they are
            _say("   Without the writer model the pages are kept as they are.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.strip() + "\n", encoding="utf-8")
        sections = text.count("\n## ") + text.startswith("## ")
        _say(f"   Wrote {target} ({sections} sections). Read it: it is what the assistant"
             " will answer from.")  # fmt: skip
        return True

    def run_all(self) -> int:
        _say("Di-Factory harness setup. Ctrl+C stops at any time; nothing is half-written.")
        self.model_key()
        reused = self.existing()
        if reused is not None:
            reused = self.read_sites(self.refresh(reused))
            if not self.try_it(reused):
                return 1
            self.go_online(reused)
            return 0
        pack_ids = self.choose_pack()
        if pack_ids is None:
            return 2
        result = self.read_sites(self.questionnaire(pack_ids))
        if not result.ok:
            _say("\nNot ready yet:")
            for problem in result.problems:
                _say(f"  - {problem}")
            for issue in result.resolved.issues if result.resolved else []:
                if issue.severity == "error":
                    _say(f"  - {issue}")
            chosen = " ".join(f"--pack {p}" for p in pack_ids)
            _say(f"Edit {result.answers_path} and run: dif-general-harness build {chosen}"
                 f" --answers {result.answers_path} --out {self.out}")  # fmt: skip
            return 1
        if not self.try_it(result):
            return 1
        self.go_online(result)
        _say("\nDone. Fine-tune it by running ./setup.sh again and reusing this client; when it"
             " is final, hand it over with /handover in Claude Code (or: dif-general-harness"
             f" handover {result.spec_path} --owner NAME --lang es).")  # fmt: skip
        _say("Chat with it any time:")
        packs = " ".join(f"--packs {p}" for p in self.packs)
        _say(f"  uv run dif-general-harness console {result.spec_path} {packs}")
        if self.gaps:
            _say("Not covered yet (in the summary, for Di-Factory): " + "; ".join(self.gaps))
        return 0


def default_packs() -> list[Path]:
    return [REPO_ROOT / "docs" / "spec" / "examples"]


def anthropic_advisor(key: str) -> ModelProvider:
    from ..providers.anthropic import AnthropicProvider

    return AnthropicProvider(ADVISOR_MODEL, api_key=key, max_tokens=1024, prompt_cache=False)


def anthropic_consultant(key: str) -> ModelProvider:
    import os

    from ..providers.anthropic import AnthropicProvider

    model = os.environ.get("DIF_CONSULTANT_MODEL") or CONSULTANT_MODEL
    return AnthropicProvider(model, api_key=key, max_tokens=2000, prompt_cache=False)


def anthropic_writer(key: str) -> ModelProvider:
    from ..providers.anthropic import AnthropicProvider

    return AnthropicProvider(DIFACTORY_DEFAULTS["main_model"], api_key=key, max_tokens=16000,
                             prompt_cache=False)  # fmt: skip


def run_setup(
    args: Any,
    run: Callable[[list[str]], int] | None = None,
    advisor: Advisor | None = anthropic_advisor,
    consultant: Advisor | None = anthropic_consultant,
    writer: Advisor | None = anthropic_writer,
) -> int:
    setup = Setup(
        args.packs or default_packs(), args.out, run=run, public_url=args.public_url,
        advisor=advisor, consultant=consultant, writer=writer,
    )  # fmt: skip
    try:
        return setup.run_all()
    except (KeyboardInterrupt, EOFError):
        _say("\nStopped. Run it again whenever you like.")
        return 130
