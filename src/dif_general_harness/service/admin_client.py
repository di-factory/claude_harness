"""Day-to-day operation of a running instance, from its server: ``dif-general-harness admin``.

What a business owner (or the Claude that helps them) does every day, as plain commands over
the instance's admin API, so nobody needs curl, tokens or JSON:

    status              is it online, what is waiting in the inbox, spend this month
    inbox               conversations handed to a person and approvals waiting
    show SESSION        a conversation, message by message
    reply SESSION TEXT  answer as a person; the customer gets it on their channel
    faq show            the FAQ exactly as the agent knows it
    faq set FILE|-      replace it (the owner's edit stands until Di-Factory ships a new one);
                        first the latest real conversations are answered again with it, and
                        it is not applied when a reply gets worse (--force applies anyway)
    faq gaps            questions customers asked that the FAQ did not answer, most asked first
    faq done|dismiss ID mark one answered (once the FAQ covers it) or not for the assistant
    costs               model spend at list prices, by day
    runs                why each run of the last week stopped, and what failed
    review [--now]      the edits the weekly review proposes (nothing is applied)
    graph NAME [QUERY]  a research graph: by state, with sources and evidence

The admin token is read from the local secrets (``~/.dif/secrets/admin_token``), never
printed; the address defaults to the instance on this machine.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx2

DEFAULT_URL = "http://127.0.0.1:8080"


class AdminError(RuntimeError):
    pass


class Admin:
    def __init__(self, client: httpx2.AsyncClient) -> None:
        self.http = client

    async def _get(self, path: str, **params: Any) -> Any:
        r = await self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        return self._json(r)

    async def _send(self, method: str, path: str, body: dict[str, Any]) -> Any:
        return self._json(await self.http.request(method, path, json=body))

    @staticmethod
    def _json(r: httpx2.Response) -> Any:
        if r.status_code == 401 or r.status_code == 403:
            raise AdminError("the admin token was refused (is ~/.dif/secrets/admin_token the"
                             " one this instance was deployed with?)")  # fmt: skip
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise AdminError(f"{r.status_code}: {detail}")
        return r.json()

    # --- the commands ---------------------------------------------------------------

    async def status(self) -> str:
        health = await self._get("/healthz")
        items = await self._get("/admin/inbox")
        month = date.today().replace(day=1).isoformat()
        spend = await self._get("/admin/costs", since=month, by="day")
        usd = float((spend.get("total") or {}).get("usd") or 0)
        lines = [
            f"Online: {health.get('instance')} (configuration {health.get('config_version')})",
            f"Waiting for a person: {len(items)}" + (" (see: admin inbox)" if items else ""),
            f"Model spend since {month}: ${usd:.2f}",
        ]
        return "\n".join(lines)

    async def inbox(self) -> str:
        items = await self._get("/admin/inbox")
        if not items:
            return "Nothing waiting: no conversation needs a person right now."
        out = []
        for i in items:
            contact = (i.get("payload") or {}).get("contact", "")
            out.append(f"- [{i['kind']}] {i['title']}"
                       + (f" (contact {contact})" if contact else "")
                       + (f"\n  conversation: {i['session']}" if i.get("session") else "")
                       + f"\n  item: {i['id']}")  # fmt: skip
        return "\n".join(out)

    async def show(self, session: str) -> str:
        data = await self._get(f"/admin/sessions/{session}")
        who = {"user": "Customer", "assistant": "Assistant"}
        lines = [f"Conversation {data['id']} ({data['state']})"]
        for m in data["messages"]:
            role = str(m["role"]).rsplit(".", 1)[-1].lower()
            lines.append(f"{who.get(role, role)}: {m['text']}")
        return "\n".join(lines)

    async def reply(self, session: str, text: str, by: str) -> str:
        await self._send("POST", f"/admin/sessions/{session}/reply", {"text": text, "by": by})
        return "Sent. The customer gets it on the channel they wrote from."

    async def _faq_docs(self, corpus: str | None) -> tuple[str, list[dict[str, Any]]]:
        if corpus is None:
            names = (await self._get("/admin/knowledge"))["corpora"]
            if not names:
                raise AdminError("this instance has no FAQ (no knowledge corpus)")
            corpus = names[0]
        docs = await self._get(f"/admin/knowledge/{corpus}/documents")
        return corpus, list(docs)

    async def faq_show(self, corpus: str | None = None) -> str:
        corpus, docs = await self._faq_docs(corpus)
        parts = []
        for doc in docs:
            full = await self._get(f"/admin/knowledge/{corpus}/document", uri=doc["uri"])
            edited = " (edited by the owner)" if full.get("origin") == "owner" else ""
            if len(docs) > 1 or edited:
                parts.append(f"<!-- {doc['uri']}{edited} -->")
            parts.append(str(full["text"]).rstrip())
        return "\n\n".join(parts) if parts else f"The {corpus} FAQ is empty."

    async def faq_set(
        self, text: str, corpus: str | None = None, uri: str | None = None, force: bool = False
    ) -> str:
        if not text.strip():
            raise AdminError("the new FAQ is empty; nothing was changed")
        corpus, docs = await self._faq_docs(corpus)
        if uri is None:
            if len(docs) != 1:
                raise AdminError(f"the {corpus} FAQ has {len(docs)} documents; say which with"
                                 " --uri (see: admin faq show)")  # fmt: skip
            uri = str(docs[0]["uri"])
        checked = "" if force else await self._check(corpus, uri, text)
        result = await self._send(
            "PUT", f"/admin/knowledge/{corpus}/documents",
            {"uri": uri, "text": text, "owner": True},
        )  # fmt: skip
        if result.get("result") == "unchanged":
            return "No change: the FAQ already says exactly that."
        return (checked + "Saved. The assistant answers with it from the next message. It"
                " stays across restarts until Di-Factory ships a new FAQ release; tell them"
                " about it so the client's answers file is updated too.")  # fmt: skip

    async def _check(self, corpus: str, uri: str, text: str) -> str:
        """The latest real conversations answered again with the new FAQ; an AdminError
        (nothing applied) when a reply gets worse."""
        try:
            r = await self.http.post(f"/admin/knowledge/{corpus}/check",
                                     json={"uri": uri, "text": text}, timeout=600.0)  # fmt: skip
        except httpx2.TimeoutException:
            return "(Not checked against real conversations: the check took too long.)\n"
        if r.status_code in (404, 501):  # an older instance, or nothing to judge with
            return "(Not checked against real conversations on this instance.)\n"
        data = self._json(r)
        worse = list(data.get("worse") or [])
        if not worse:
            return f"Checked against recent real conversations: {data.get('summary')}.\n"
        lines = [f"Not applied: with this FAQ, {len(worse)} reply(ies) to real customer"
                 " messages got worse:"]  # fmt: skip
        for turn in worse[:5]:
            lines += [f"  - Customer: {_short(turn.get('customer'))}",
                      f"    before: {_short(turn.get('before'))}",
                      f"    after:  {_short(turn.get('after'))}",
                      f"    why:    {_short(turn.get('why'))}"]  # fmt: skip
        lines.append("Fix the FAQ and try again, or apply it anyway: admin faq set FILE --force")
        raise AdminError("\n".join(lines))

    async def faq_gaps(self, everything: bool = False) -> str:
        gaps = await self._get("/admin/knowledge/gaps", status="all" if everything else "open")
        if not gaps:
            return ("No open questions: everything customers asked was in the FAQ."
                    if not everything else "No questions recorded yet.")  # fmt: skip
        lines = [f"{len(gaps)} question(s) customers asked that the FAQ did not answer"
                 " (most asked first):"]  # fmt: skip
        for gap in gaps:
            day = datetime.fromtimestamp(float(gap["last_seen"])).strftime("%Y-%m-%d")
            state = "" if gap["status"] == "open" else f" [{gap['status']}]"
            lines.append(f"  {gap['id']}  x{gap['asked']}  last {day}{state}  {gap['question']}")
        lines.append("Add the answers to the FAQ (admin faq show > faq.md; edit; admin faq set"
                     " faq.md), then mark each one: admin faq done ID (or dismiss ID when the"
                     " assistant should not answer it).")  # fmt: skip
        return "\n".join(lines)

    async def faq_mark(self, gap_id: str, status: str) -> str:
        await self._send("POST", "/admin/knowledge/gaps", {"id": gap_id, "status": status})
        if status == "answered":
            return f"{gap_id} marked answered; if a customer asks it again, it opens again."
        return f"{gap_id} dismissed: it stays out of the list."

    async def runs(self, days: float = 7.0) -> str:
        records = await self._get("/admin/records", days=days)
        if not records:
            return f"No runs in the last {days:g} day(s)."
        out = []
        for r in records:
            when = datetime.fromtimestamp(float(r["ended"])).strftime("%Y-%m-%d %H:%M")
            counts = ", ".join(f"{k} {v}" for k, v in (r.get("counts") or {}).items())
            out.append(f"- {when} {r['kind']} {r['name']}: {r['stop_reason']}"
                       + (f" ({counts})" if counts else ""))  # fmt: skip
            for f in (r.get("failures") or [])[:3]:
                out.append(f"    {f.get('gate')}: {f.get('item', '')} - {_short(f.get('reason'))}")
        return "\n".join(out)

    async def review(self, now: bool = False) -> str:
        if now:
            done = await self._send("POST", "/admin/review", {})
            if not done.get("proposals"):
                return "Reviewed the last week: nothing repeated enough to propose an edit."
        items = await self._get("/admin/inbox", kind="proposal")
        if not items:
            return "No proposed edits waiting (the review runs weekly; --now runs it now)."
        out = []
        for item in items:
            out.append(f"# {item['title']} (item {item['id']})")
            for p in (item.get("payload") or {}).get("proposals") or []:
                out += [f"\n## {p['target']}: {p.get('why', '')}", p.get("diff", "")]
        out.append("\nNothing was changed. Apply what you accept with the setup or adjust;"
                   " then close the item: admin inbox.")  # fmt: skip
        return "\n".join(out)

    async def graph(self, name: str, query: str | None = None) -> str:
        if query:
            found = await self._get(f"/admin/graphs/{name}", q=query)
            if not found:
                return f"Nothing in {name} matches {query!r}."
            out = []
            for n in found:
                out.append(f"- {n['label']} ({n['type']}, {n['state']}, confidence"
                           f" {float(n['confidence']):.2f})")  # fmt: skip
                out += [f"    source: {s.get('url')}" for s in n.get("sources", [])[:3]]
                out += [f"    {e['from']} --{e['type']}--> {e['to']}: {e['evidence']}"
                        for e in n.get("edges", [])[:5]]  # fmt: skip
            return "\n".join(out)
        r = await self.http.get(f"/admin/graphs/{name}", params={"format": "md"})
        if r.status_code >= 400:
            raise AdminError(f"{r.status_code}: {r.text[:200]}")
        return r.text

    async def costs(self, since: str | None = None) -> str:
        data = await self._get("/admin/costs", since=since, by="day")
        lines = [f"Model spend {data['since']} to {data['until']} (list prices):"]
        for row in data["rows"]:
            lines.append(f"  {row['day']}: ${float(row.get('usd') or 0):.4f}"
                         f" ({row.get('calls', 0)} calls)")  # fmt: skip
        lines.append(f"Total: ${float((data.get('total') or {}).get('usd') or 0):.4f}")
        return "\n".join(lines)


def _short(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _read(target: str) -> str:
    if target == "-":
        return sys.stdin.read()
    try:
        return Path(target).read_text(encoding="utf-8")
    except OSError as exc:
        raise AdminError(f"cannot read {target}: {exc.strerror}") from None


def admin_client(url: str | None, token: str | None) -> httpx2.AsyncClient:
    if not token:
        raise AdminError("no admin token: it is ~/.dif/secrets/admin_token on the server"
                         " (dif-general-harness secrets set admin_token)")  # fmt: skip
    base = url or os.environ.get("DIF_ADMIN_URL") or DEFAULT_URL
    return httpx2.AsyncClient(
        base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
    )


async def run(admin: Admin, args: Any) -> str:
    cmd = args.command
    if cmd == "status":
        return await admin.status()
    if cmd == "inbox":
        return await admin.inbox()
    if cmd == "show":
        return await admin.show(args.session)
    if cmd == "reply":
        return await admin.reply(args.session, args.text, args.by)
    if cmd == "costs":
        return await admin.costs(args.since)
    if cmd == "runs":
        return await admin.runs(args.days)
    if cmd == "review":
        return await admin.review(args.now)
    if cmd == "graph":
        return await admin.graph(args.name, args.query)
    if cmd == "faq":
        action, target = args.action, getattr(args, "target", "-")
        if action == "show":
            return await admin.faq_show(args.corpus)
        if action == "gaps":
            return await admin.faq_gaps(bool(getattr(args, "all", False)))
        if action in ("done", "dismiss"):
            if not target or target == "-":
                raise AdminError(f"which one? admin faq {action} ID (see: admin faq gaps)")
            return await admin.faq_mark(target, "answered" if action == "done" else "dismissed")
        source = getattr(args, "file", None)
        text = source.read() if source is not None else _read(target)
        return await admin.faq_set(text, args.corpus, args.uri, bool(getattr(args, "force", False)))
    raise AdminError(f"unknown command {cmd!r}")
