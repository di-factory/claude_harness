"""Day-to-day operation of a running instance, from its server: ``dif-general-harness admin``.

What a business owner (or the Claude that helps them) does every day, as plain commands over
the instance's admin API, so nobody needs curl, tokens or JSON:

    status              is it online, what is waiting in the inbox, spend this month
    inbox               conversations handed to a person and approvals waiting
    show SESSION        a conversation, message by message
    reply SESSION TEXT  answer as a person; the customer gets it on their channel
    faq show            the FAQ exactly as the agent knows it
    faq set FILE|-      replace it (the owner's edit stands until Di-Factory ships a new one)
    costs               model spend at list prices, by day

The admin token is read from the local secrets (``~/.dif/secrets/admin_token``), never
printed; the address defaults to the instance on this machine.
"""

from __future__ import annotations

import os
from datetime import date
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

    async def faq_set(self, text: str, corpus: str | None = None, uri: str | None = None) -> str:
        if not text.strip():
            raise AdminError("the new FAQ is empty; nothing was changed")
        corpus, docs = await self._faq_docs(corpus)
        if uri is None:
            if len(docs) != 1:
                raise AdminError(f"the {corpus} FAQ has {len(docs)} documents; say which with"
                                 " --uri (see: admin faq show)")  # fmt: skip
            uri = str(docs[0]["uri"])
        result = await self._send(
            "PUT", f"/admin/knowledge/{corpus}/documents",
            {"uri": uri, "text": text, "owner": True},
        )  # fmt: skip
        if result.get("result") == "unchanged":
            return "No change: the FAQ already says exactly that."
        return ("Saved. The assistant answers with it from the next message. It stays across"
                " restarts until Di-Factory ships a new FAQ release; tell them about it so the"
                " client's answers file is updated too.")  # fmt: skip

    async def costs(self, since: str | None = None) -> str:
        data = await self._get("/admin/costs", since=since, by="day")
        lines = [f"Model spend {data['since']} to {data['until']} (list prices):"]
        for row in data["rows"]:
            lines.append(f"  {row['day']}: ${float(row.get('usd') or 0):.4f}"
                         f" ({row.get('calls', 0)} calls)")  # fmt: skip
        lines.append(f"Total: ${float((data.get('total') or {}).get('usd') or 0):.4f}")
        return "\n".join(lines)


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
    if cmd == "faq":
        if args.action == "show":
            return await admin.faq_show(args.corpus)
        return await admin.faq_set(args.file.read(), args.corpus, args.uri)
    raise AdminError(f"unknown command {cmd!r}")
