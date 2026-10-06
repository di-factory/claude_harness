# ruff: noqa: E501 - the documents below are Markdown: tables and prose keep their lines
"""Handing a running solution to its client: the documents a new Claude starts from.

``dif-general-harness handover INSTANCE.json`` writes, on the client's server, a folder the
client opens Claude Code in (``cd ~/<client>; claude``). Everything that Claude needs to help
the business owner run their assistant is there, so nobody has to explain it again:

- ``CLAUDE.md``: who it works for, the business in brief, the solution and its addresses,
  the commands, and the rules it must not break (instructions in English; it speaks with the
  owner in their language);
- ``docs/``: the business (``negocio.md``, from the owner's own answers), the solution
  (``solucion.md``), daily operation (``operacion.md``), what is not connected yet and what
  that means (``pendientes.md``), what the owner can change and what goes through Di-Factory
  (``cambios.md``);
- ``GUIA.md``: a one-page guide for the owner, in their language;
- ``negocio``: the one command Claude runs (``admin`` against the instance on this server);
- ``.claude/settings.json`` (what Claude may and may not run) and ``.claude/skills/``
  (status, inbox, replies, FAQ, costs);
- ``HANDOVER.md``: Di-Factory's checklist, with what could be checked automatically (no
  approver key left on the client's server, the admin token in place...).

It reads only the instance, its packs and the secrets' *presence*, never their values.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..channels.landing import faq_sections
from ..spec.loader import PackCatalog, load_instance
from ..spec.schema import SolutionSpec
from ..tenancy import local_backend
from .impact import missing

LANGUAGES = {"es": "Spanish", "en": "English", "pt": "Portuguese", "fr": "French"}
SKILLS = ("estado", "bandeja", "responder", "faq", "preguntas", "costos")


@dataclass
class Handover:
    folder: Path
    files: list[Path] = field(default_factory=list)
    checks: list[tuple[bool, str]] = field(default_factory=list)  # (ok, what)

    @property
    def ready(self) -> bool:
        return all(ok for ok, _ in self.checks)


def _write(out: Handover, rel: str, text: str, mode: int | None = None) -> None:
    path = out.folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    out.files.append(path)


def _hours(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{k} {v}" for k, v in value.items())
    return str(value or "not given")


def _secret_present(name: str, deployed: Path | None) -> bool:
    if deployed is not None and (deployed / name).exists():
        return True
    return bool(local_backend().get(name))


def build_handover(
    instance_path: Path, catalog: PackCatalog, out_dir: Path, *,
    url: str | None = None, owner: str | None = None, lang: str | None = None,
    support: str | None = None, harness: Path | None = None, deploy_root: Path | None = None,
) -> Handover:  # fmt: skip
    resolved = load_instance(instance_path, catalog)
    spec, data = resolved.spec, resolved.data
    values = data.get("values") or {}
    name = str(values.get("business_name") or (spec.tenant.name if spec.tenant else "")
               or spec.solution.name)  # fmt: skip
    owner = owner or "the business owner"
    lang = (lang or (spec.solution.locale or "en"))[:2]
    language = LANGUAGES.get(lang, lang)
    support = support or "Di-Factory (the contact in your contract)"
    harness = (harness or Path(__file__).resolve().parents[3]).resolve()
    sid = spec.solution.id
    deployed = (deploy_root or harness / "deploy" / "build") / sid / "secrets"
    deployed_dir = deployed if deployed.is_dir() else None
    site = (url or os.environ.get("DIF_PUBLIC_URL") or "").rstrip("/")
    out = Handover(out_dir.expanduser().resolve())
    out.folder.mkdir(parents=True, exist_ok=True)

    sections = faq_sections(spec, data)
    pending = [k for k, v in values.items() if v == "pending"]
    gaps = missing(spec, data, lambda n: _secret_present(n, deployed_dir), pending)
    web = [n for n, c in spec.channels.items() if c.type == "web"]
    ctx = _Context(spec, data, name, owner, lang, language, support, harness, sid, site,
                   sections, gaps, web, instance_path.resolve(), out.folder)  # fmt: skip

    _write(out, "CLAUDE.md", ctx.claude_md())
    _write(out, "docs/negocio.md", ctx.business())
    _write(out, "docs/solucion.md", ctx.solution())
    _write(out, "docs/operacion.md", ctx.operation())
    _write(out, "docs/pendientes.md", ctx.pending())
    _write(out, "docs/cambios.md", ctx.changes())
    _write(out, "GUIA.md", ctx.guide())
    _write(out, "negocio", ctx.wrapper(), mode=0o755)
    _write(out, ".claude/settings.json", json.dumps(ctx.settings(), indent=2))
    for skill in SKILLS:
        _write(out, f".claude/skills/{skill}/SKILL.md", ctx.skill(skill))
    for folder in ("borradores", "solicitudes"):
        (out.folder / folder).mkdir(exist_ok=True)

    home = Path.home()
    out.checks = [
        (not (home / ".dif" / "keys" / "jag.key").exists(),
         "Jag's approver key is not on this server (~/.dif/keys/jag.key): deploys are signed"
         " by Di-Factory, never on a client's machine"),
        (_secret_present("admin_token", deployed_dir),
         "the admin token is in place (the `negocio` command needs it)"),
        (_secret_present("anthropic", deployed_dir),
         "a model key is set; make sure it is the client's own (their workspace, their bill)"),
        (shutil.which("claude") is not None,
         "Claude Code is installed (npm install -g @anthropic-ai/claude-code, then `claude`"
         " and the client logs in with their own account)"),
        (bool(site), "the public address is known (pass --url https://...)"),
    ]  # fmt: skip
    _write(out, "HANDOVER.md", ctx.checklist(out.checks))
    return out


@dataclass
class _Context:
    spec: SolutionSpec
    data: dict[str, Any]
    name: str
    owner: str
    lang: str
    language: str
    support: str
    harness: Path
    sid: str
    site: str
    sections: list[tuple[str, str]]
    gaps: list[Any]
    web: list[str]
    instance: Path
    folder: Path

    # --- the documents ----------------------------------------------------------------

    def claude_md(self) -> str:
        intro = self.sections[0][1].strip() if self.sections else "(no description yet)"
        site = self.site or "(ask Di-Factory for the address)"
        return f"""# CLAUDE.md: {self.name}

You help **{self.owner}**, who runs **{self.name}**, look after their customer assistant:
an AI agent that answers their customers on the web (and other channels once connected),
built and maintained by Di-Factory. Speak with {self.owner} in **{self.language}**, plainly,
without technical words unless they ask. They are not a programmer.

## The business, in brief
{intro}

Full details: `docs/negocio.md` (it is the assistant's own FAQ: what it knows is what it says).

## The assistant
- Public page: {site}/ (the business's landing page) · chat: {site}/chat
- What it does and how: `docs/solucion.md`. What is not connected yet, and what that means:
  `docs/pendientes.md`.

## How you operate it
Everything goes through one command, run from this folder (never curl, never tokens):

| What | Command |
|---|---|
| Is it working? What is waiting? | `./negocio status` |
| Conversations handed to a person | `./negocio inbox` |
| Read one conversation | `./negocio show SESSION` |
| Answer a customer as the owner | `./negocio reply SESSION "text"` |
| The FAQ, exactly as the assistant knows it | `./negocio faq show` |
| Replace the FAQ | `./negocio faq set borradores/faq.md` |
| Questions customers asked that the FAQ did not answer | `./negocio faq gaps` |
| Mark one answered (or not for the assistant) | `./negocio faq done ID` (`dismiss ID`) |
| Model spend by day | `./negocio costs` |

Routines, step by step: `docs/operacion.md`. Skills in `.claude/skills/` cover the usual
requests (estado, bandeja, responder, faq, costos).

## Rules you never break
1. **Never send anything to a customer without {self.owner}'s explicit OK** on the exact text.
   Show the draft first; send only after they say yes.
2. **Never read, print or move secrets or keys** (`~/.dif/`, `deploy/build/*/secrets/`), and
   never ask {self.owner} to paste one into the chat.
3. **Never change the solution itself**: the harness (`{self.harness}`), the instance and
   pack files, Docker, Caddy, the server. Those are signed by Di-Factory; a change there
   would stop the next deploy. If something needs one, write a request in `solicitudes/`
   (see `docs/cambios.md`) and tell {self.owner} to send it to {self.support}.
4. **The FAQ is the truth the assistant tells customers.** Change it only with {self.owner}'s
   words and OK; never invent prices, policies or promises. Keep a copy before every change
   (`./negocio faq show > borradores/faq-YYYY-MM-DD.md`).
5. **Customer data stays here.** Quote only what is needed; never copy conversations
   elsewhere.
6. If `./negocio status` fails or the page is down, do not try to repair the server: tell
   {self.owner} to contact {self.support} with the exact error.

## When in doubt
Ask {self.owner}. If it is about the platform, it is Di-Factory's: write it down in
`solicitudes/` and say so.
"""

    def business(self) -> str:
        values = self.data.get("values") or {}
        lines = [f"# {self.name}", "", "What the assistant knows and tells customers. Source:"
                 " the owner's answers when the solution was built; current version on the"
                 " server: `./negocio faq show`.", ""]  # fmt: skip
        tenant = self.spec.tenant
        lines += [
            f"- Language of the customers: {self.spec.solution.locale or 'en'}",
            f"- Time zone: {tenant.timezone if tenant else 'not given'}",
            f"- Opening hours: {_hours(values.get('business_hours'))}",
        ]
        if values.get("whatsapp_number") and values.get("whatsapp_number") != "pending":
            lines.append(f"- WhatsApp number: {values['whatsapp_number']}")
        lines.append("")
        for heading, body in self.sections:
            lines += [f"## {heading}", body.strip(), ""]
        if not self.sections:
            lines.append("(No FAQ yet.)")
        return "\n".join(lines)

    def solution(self) -> str:
        spec = self.spec
        sol = spec.solution
        out = [f"# The solution: {sol.name or sol.id}", "",
               f"Instance `{sol.id}`, built on Di-Factory's pack(s): "
               + ", ".join(f"`{e}`" for e in spec.extends) + ".", ""]  # fmt: skip
        out += ["## Agents", ""]
        for agent_name, agent in spec.agents.items():
            out.append(f"- **{agent_name}**: {agent.description or 'no description'}")
        out += ["", "## Channels (where customers reach it)", ""]
        for cname, ch in spec.channels.items():
            where = ""
            if ch.type == "web":
                where = f" — {self.site}/chat" if self.site else ""
                kind = "web chat" + (" (public)" if ch.public else " (needs an access code)")
            elif ch.type == "gateway":
                kind = f"WhatsApp/SMS ({ch.provider or 'gateway'})"
                where = f" — {ch.address}" if ch.address else ""
            else:
                kind = ch.type
            out.append(f"- **{cname}**: {kind}{where}")
        if self.web and self.site:
            out.append(f"- The landing page {self.site}/ is built from the FAQ; changing the"
                       " FAQ changes the page.")  # fmt: skip
        out += ["", "## How it behaves", "",
                "- It answers only from the FAQ (`docs/negocio.md`); when the FAQ does not"
                " cover a question it says so and hands the conversation to a person"
                " (it appears in `./negocio inbox`). That is a guardrail, not an error: the"
                " fix is to add the answer to the FAQ.",
                "- A conversation handed to a person stays with a person: the assistant does"
                " not answer it again; the owner replies with `./negocio reply`."]  # fmt: skip
        esc = spec.policies.escalation or {}
        for rule in esc.get("rules") or []:
            out.append(f"- Escalates to a person when: `{rule.get('when')}`")
        deny = spec.policies.permissions.deny
        if deny:
            out.append(f"- Never allowed to: {', '.join(deny)}")
        day = spec.policies.budgets.get("per_tenant_day") or {}
        usd = day.get("usd") if isinstance(day, dict) else None
        if usd:
            out.append(f"- Spend limit: ${usd} per day in model costs; past it, it hands"
                       " conversations to a person.")  # fmt: skip
        gov = spec.governance
        out += ["", "## Privacy", ""]
        if gov.pii.tokenize:
            out.append("- Personal data in conversations is tokenized before it reaches the"
                       f" model ({', '.join(gov.pii.classes)}).")  # fmt: skip
        if gov.retention:
            kept = ", ".join(f"{k} {v}" for k, v in gov.retention.items() if v)
            out.append(f"- Retention: {kept}.")
        if gov.consent.required:
            out.append(f"- Consent is required on: {', '.join(gov.consent.channels)}.")
        return "\n".join(out)

    def operation(self) -> str:
        return f"""# Daily operation

All commands run from this folder. They talk to the assistant on this server.

## Every morning (2 minutes)
1. `./negocio status`: is it online, how many conversations wait for a person, spend so far.
2. If anything waits: `./negocio inbox`, then for each one `./negocio show SESSION`.
3. Tell {self.owner} who wrote, what they asked, and propose a reply (see below).

## Answering a customer as the owner
1. Read the conversation: `./negocio show SESSION`.
2. Draft the reply in the customer's language, short and kind, with only what {self.owner}
   confirms (prices, availability, promises: only theirs).
3. Show the exact text to {self.owner}. Only after their OK:
   `./negocio reply SESSION "the text"`.
4. If the question will come back, propose adding it to the FAQ.

## Changing the FAQ (prices, services, hours, policies)
1. Copy the current one: `./negocio faq show > borradores/faq-YYYY-MM-DD.md`.
2. Copy it again to `borradores/faq.md` and change only what {self.owner} asked, with their
   words. Keep the `## ` headings: each one is a question customers ask.
3. Show {self.owner} what changes (before → after) and get their OK.
4. `./negocio faq set borradores/faq.md`. Before it goes live, the latest real conversations
   are answered again with the new FAQ; if a reply gets worse, it is **not applied** and you
   see the customer's message with the reply before and after. Fix the FAQ and try again;
   `--force` applies it anyway, only with {self.owner}'s explicit OK after seeing them.
   Once applied, the assistant answers with it from the next message, and the landing page
   shows it.
5. Write the change in `solicitudes/faq-cambios.md` (date and what) so Di-Factory carries it
   into the next release of the solution; their next release replaces the live FAQ.

## Questions the FAQ did not answer (once a week)
1. `./negocio faq gaps` lists what customers asked that the FAQ did not cover, most asked
   first, in their own words.
2. For each one {self.owner} can answer, add it to the FAQ (above), with their words.
3. Then `./negocio faq done ID`. If it is something the assistant should not answer (a
   medical question, a joke), `./negocio faq dismiss ID`. A question marked done that
   customers keep asking comes back to the list: the FAQ still does not answer it.

## Costs
`./negocio costs` shows model spend by day at list prices. A normal conversation turn costs
a fraction of a cent. If a day looks unusual, look at that day's conversations.

## If something is wrong
- `./negocio status` fails, or the page does not open: do not repair the server. Tell
  {self.owner} to send the exact error to {self.support}.
- The assistant answers something wrong: find the conversation, fix the FAQ if the FAQ is
  wrong; if the FAQ is right and the assistant still errs, write it in `solicitudes/` for
  Di-Factory.
"""

    def pending(self) -> str:
        if not self.gaps:
            return "# Not connected yet\n\nEverything the solution uses is connected."
        lines = ["# Not connected yet, and what that means", "",
                 "The assistant works without these; this is what stays off. Connecting them"
                 " is done with Di-Factory (they install the keys; never paste a key into"
                 " this chat).", ""]  # fmt: skip
        for item in self.gaps:
            lines.append(f"## {item.name}: {item.what}")
            lines += [f"- Without it: {i}" for i in item.impact or ["nothing visible"]]
            how = item.how.replace("INSTANCE.json", str(self.instance))
            lines += [f"- How to get it: {how}", ""]
        return "\n".join(lines)

    def changes(self) -> str:
        return f"""# What {self.owner} changes, and what Di-Factory changes

| Change | Who | How |
|---|---|---|
| Prices, services, hours, policies, any FAQ answer | {self.owner} (with you) | `./negocio faq set` (see operacion.md) |
| Replying to customers handed to a person | {self.owner} (with you) | `./negocio reply` |
| Connecting WhatsApp, Google Calendar, email | Di-Factory | request (below); they install the keys |
| The page's look (logo, colors) | Di-Factory | request with the logo or brand files |
| What the assistant may do, its rules, budgets, channels | Di-Factory | request |
| Platform updates, the server, backups | Di-Factory | automatic or by request |

## Writing a request
Create `solicitudes/YYYY-MM-DD-short-title.md` with:

```
What: (one sentence)
Why: (what the business needs, an example conversation if there is one)
Urgency: (today / this week / when possible)
```

Then tell {self.owner} to send it to {self.support}.
"""

    def guide(self) -> str:
        site = self.site or "(la dirección que te dio Di-Factory)"
        if self.lang == "es":
            return f"""# Guía rápida: tu asistente de {self.name}

**Tu página:** {site}/ · **Chat:** {site}/chat

## Cómo hablar con Claude, tu ayudante
1. Entra al servidor (Di-Factory te dio el acceso) y escribe: `cd {self.sid_folder}` y luego `claude`.
2. Háblale normal, en español. Por ejemplo:
   - «¿Cómo va todo hoy?» → te dice si está en línea y si alguien espera respuesta.
   - «¿Quién me escribió?» → te muestra las conversaciones que esperan a una persona.
   - «Contéstale a Ana que sí tenemos su talla» → te enseña el texto y lo envía solo si dices que sí.
   - «Cambia el precio de las pruebas a 300 pesos» → te muestra el cambio y lo aplica cuando lo apruebes.
   - «¿Cuánto llevamos gastado este mes?»
3. Para salir: escribe `/exit`.

## Lo que hace tu asistente
Contesta a tus clientes con lo que dice tu FAQ; si no sabe algo, no inventa: te pasa la
conversación (la ves con «¿quién me escribió?»).

## Lo que hace Di-Factory
Conectar WhatsApp o tu calendario, cambiar el diseño, reglas o actualizaciones. Claude te
ayuda a escribir la solicitud. Contacto: {self.support}.

**Nunca pegues una clave o contraseña en el chat.**
"""
        return f"""# Quick guide: your {self.name} assistant

**Your page:** {site}/ · **Chat:** {site}/chat

## Talking to Claude, your helper
1. Log in to the server (Di-Factory gave you access), then: `cd {self.sid_folder}` and `claude`.
2. Talk normally. For example:
   - "How is everything today?" → online status and who is waiting for an answer.
   - "Who wrote to me?" → the conversations waiting for a person.
   - "Tell Ana we have her size" → shows you the text and sends it only if you say yes.
   - "Change the fitting price to 300" → shows the change and applies it once you approve.
   - "How much have we spent this month?"
3. To leave: type `/exit`.

## What your assistant does
It answers your customers from your FAQ; when it does not know, it does not invent: it hands
you the conversation.

## What Di-Factory does
Connecting WhatsApp or your calendar, changing the design, rules, or updates. Claude helps
you write the request. Contact: {self.support}.

**Never paste a key or password into the chat.**
"""

    @property
    def sid_folder(self) -> str:
        home = Path.home().resolve()
        return ("~/" + str(self.folder.relative_to(home))
                if self.folder.is_relative_to(home) else str(self.folder))  # fmt: skip

    def wrapper(self) -> str:
        return f"""#!/usr/bin/env bash
# Operate {self.name}'s assistant on this server: ./negocio status | inbox | show | reply | faq | costs
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
exec uv run --quiet --project "{self.harness}" dif-general-harness admin "$@"
"""

    def settings(self) -> dict[str, Any]:
        harness = str(self.harness)
        return {
            "permissions": {
                "allow": [
                    "Bash(./negocio status)", "Bash(./negocio inbox)", "Bash(./negocio show:*)",
                    "Bash(./negocio faq show:*)", "Bash(./negocio faq gaps:*)",
                    "Bash(./negocio costs:*)",
                    "Read(./**)", "Edit(./borradores/**)", "Write(./borradores/**)",
                    "Edit(./solicitudes/**)", "Write(./solicitudes/**)",
                ],
                "ask": ["Bash(./negocio reply:*)", "Bash(./negocio faq set:*)",
                        "Bash(./negocio faq done:*)", "Bash(./negocio faq dismiss:*)"],
                "deny": [
                    "Read(~/.dif/**)", f"Read({harness}/deploy/build/**/secrets/**)",
                    f"Edit({harness}/**)", f"Write({harness}/**)",
                    "Bash(sudo:*)", "Bash(docker:*)", "Bash(rm:*)", "Bash(git push:*)",
                    "Bash(curl:*)", "Bash(cat ~/.dif:*)",
                ],
            }
        }  # fmt: skip

    def skill(self, name: str) -> str:
        bodies = {
            "estado": (
                "How the assistant is doing today: online, what waits, spend.",
                "1. Run `./negocio status`.\n2. Say it in one or two plain sentences."
                " If something waits for a person, offer to show it (skill bandeja)."
                "\n3. If the command fails, do not try to fix the server: give the"
                f" owner the exact error to send to {self.support}.",
            ),
            "bandeja": (
                "Conversations handed to a person and what each customer wants.",
                "1. Run `./negocio inbox`.\n2. For each item, `./negocio show"
                " SESSION` and summarize: who, what they asked, since when.\n3."
                " Offer a reply for each (skill responder). Never send without the"
                " owner's OK.",
            ),
            "responder": (
                "Answer a customer as the owner, with their explicit OK.",
                "1. Read the conversation (`./negocio show SESSION`).\n2. Draft a"
                " short, kind reply in the customer's language, using only facts"
                " the owner confirms.\n3. Show the exact text and ask: ¿lo envío?"
                " / send it?\n4. Only after a clear yes: `./negocio reply SESSION"
                ' "text"`.\n5. If the question will come back, propose adding it'
                " to the FAQ (skill faq).",
            ),
            "faq": (
                "Show or change what the assistant knows (prices, services, hours...).",
                "1. `./negocio faq show > borradores/faq-$(date +%F).md` (a copy first)."
                "\n2. Copy it to `borradores/faq.md`; change only what the owner asked,"
                " with their words; keep the `## ` headings.\n3. Show before → after and"
                " get an explicit OK.\n4. `./negocio faq set borradores/faq.md`. If it says"
                " **Not applied** (a reply to a real customer got worse), show the owner"
                " the before and after, fix the FAQ and try again; use `--force` only with"
                " the owner's explicit OK.\n5. Add"
                " a line to `solicitudes/faq-cambios.md` (date, what) for Di-Factory."
                "\nNever invent prices, policies or promises.",
            ),
            "preguntas": (
                "What customers asked that the FAQ did not answer, and adding the answers.",
                "1. `./negocio faq gaps`.\n2. Summarize: the most asked first, in the"
                " customers' words.\n3. Ask the owner for the answer to each one they"
                " want covered (never invent it).\n4. Add the answers to the FAQ (skill"
                " faq), then `./negocio faq done ID` for each one covered, or"
                " `./negocio faq dismiss ID` for what the assistant should not answer;"
                " both only with the owner's OK.",
            ),
            "costos": (
                "Model spend by day, and whether anything looks unusual.",
                "1. `./negocio costs` (or `--since YYYY-MM-DD`).\n2. Give the month's"
                " total and the busiest day in one sentence; a normal turn costs a"
                " fraction of a cent.",
            ),
        }
        what, how = bodies[name]
        return f"""---
name: {name}
description: {what}
---

{how}

See `CLAUDE.md` for the rules and `docs/operacion.md` for the routines.
"""

    def checklist(self, checks: list[tuple[bool, str]]) -> str:
        rows = "\n".join(f"- [{'x' if ok else ' '}] {what}" for ok, what in checks)
        return f"""# Handover checklist: {self.name} ({self.sid})

For Di-Factory. Generated on the client's server; the boxes below were checked
automatically when it was written (run `dif-general-harness handover` again to re-check).

## Checked automatically
{rows}

## To do by hand
- [ ] The server is in the client's AWS account (or billed to them), with their Elastic IP.
- [ ] SSH (or AWS Session Manager) access is the client's; Di-Factory's keys are removed from
      `~/.ssh/authorized_keys`.
- [ ] The model key is the client's own (their Anthropic workspace, with a spending limit):
      `dif-general-harness secrets set anthropic`, then `./setup.sh` (reuse the client).
- [ ] A new admin token only the client knows (`dif-general-harness secrets set admin_token`,
      then `./setup.sh` to copy it in).
- [ ] Twilio, Google and other accounts are the client's (see `docs/pendientes.md`).
- [ ] Backups: a daily EBS snapshot (AWS Data Lifecycle Manager) of the server's volume.
- [ ] A health alert on {self.site or "https://<address>"}/healthz (e.g. Route 53 health check).
- [ ] Claude Code: installed, the client logged in with their own account, and a first
      session done together in this folder (`claude`, then «¿cómo va todo?»).
- [ ] Optional: register the instance with the control plane (`fleet register`) so updates
      arrive as signed offers.
"""
