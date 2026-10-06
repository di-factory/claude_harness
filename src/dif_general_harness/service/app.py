"""The HTTP service (FastAPI): channel webhooks, trigger webhooks, the admin API.

Routes:
- ``GET /healthz``: liveness and config version; ``GET /readyz``: the database answers.
- ``POST /channels/{name}``: inbound messages. Gateway and Telegram requests are verified
  and queued (acknowledged at once); REST/web channels answer inline, and voice answers
  inline with TwiML (speak, listen, transfer).
- ``GET /``: the business's landing page (from its FAQ) with the chat, when it has a web chat;
- ``GET /chat`` (or ``/chat/{name}``): the web chat page of a ``web`` channel;
  ``GET /channels/{name}/outbox``: replies that arrived later (a person, a reminder).
- ``POST /hooks/{path}``: webhook triggers, verified with their shared secret
  (``X-Hub-Signature-256`` or a bearer token), deduplicated by delivery id.
- ``/admin/*``: the inbox (list, decide), sessions (view, reply as a person), consent,
  audit verification, spend and job counts, running file and batch triggers. Bearer
  ``admin_token``; without one the admin API is off.

The worker (queue lanes) runs inside the same process by default: one container per
instance (ARCHITECTURE §3.20).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hmac
import json
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from ..channels import ChannelError, Handshake, Inbound, RateLimited, Unauthorized, WebChannel
from ..channels.landing import favicon, render_landing
from ..channels.web_page import render_chat
from ..knowledge.store import GAP_STATUSES
from ..observability import quality
from ..runtime import Instance
from ..tenancy.config_versions import ConfigError, ConfigStore
from .config import apply_active, watch_config
from .headless import Headless


class Decision(BaseModel):
    approved: bool
    note: str = ""
    by: str = "operator"
    text: str | None = None  # a proposed constraint, reworded by the person


class Feedback(BaseModel):
    rating: Literal["up", "down"]
    comment: str = ""
    by: str = "operator"


class NewConstraint(BaseModel):
    text: str
    agent: str = "*"
    by: str = "operator"


class OperatorReply(BaseModel):
    text: str
    by: str = "operator"


class ConsentChange(BaseModel):
    contact: str
    channel: str
    status: Literal["granted", "revoked"]
    source: str = "admin"


def _inbound(request: Request, body: bytes) -> Inbound:
    return Inbound(
        url=str(request.url),
        headers={k.lower(): v for k, v in request.headers.items()},
        body=body,
        client=request.client.host if request.client else "",
    )


def create_app(
    headless: Headless,
    *,
    admin_token: str | None = None,
    run_worker: bool = True,
    worker_lanes: int = 4,
    config_poll_s: float | None = 15.0,
    background: Sequence[Callable[[asyncio.Event], Coroutine[Any, Any, None]]] | None = None,
) -> FastAPI:
    scope = headless.instance.scope

    def current() -> Instance:  # the running instance changes on a config reload
        return headless.instance

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        tasks: list[asyncio.Task[None]] = []
        if run_worker:
            await headless.start()
            tasks.append(asyncio.create_task(headless.worker(concurrency=worker_lanes).run(stop)))
        if config_poll_s:
            tasks.append(asyncio.create_task(watch_config(headless, stop, config_poll_s)))
        tasks += [asyncio.create_task(job(stop)) for job in background or []]
        try:
            yield
        finally:
            stop.set()
            await asyncio.gather(*tasks)

    app = FastAPI(title="dif-general-harness", lifespan=lifespan, docs_url=None, redoc_url=None)

    def admin(request: Request) -> None:
        if not admin_token:
            raise HTTPException(403, "the admin API is disabled (no admin token)")
        auth = request.headers.get("authorization", "")
        if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), admin_token)):
            raise HTTPException(401, "invalid admin token")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "tenant": scope.tenant_id,
            "instance": scope.instance_id,
            "config_version": current().resolved.version_hash,
        }

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        try:
            await current().db.fetchone("SELECT 1 AS ok")
        except Exception as exc:
            raise HTTPException(503, f"database unavailable: {type(exc).__name__}") from None
        return {"status": "ready"}

    @app.post("/channels/{name}")
    async def channel(name: str, request: Request) -> Response:
        try:
            results = await headless.receive(name, _inbound(request, await request.body()))
        except Unauthorized as exc:
            raise HTTPException(401, str(exc)) from None
        except RateLimited as exc:
            raise HTTPException(429, str(exc)) from None
        except Handshake as shake:
            return Response(json.dumps(shake.body), media_type="application/json")
        except ChannelError as exc:
            raise HTTPException(400, str(exc)) from None
        adapter = headless.adapters[name]
        respond = getattr(adapter, "respond", None)
        if results is not None and respond is not None:  # voice: TwiML, not JSON
            content, media_type = respond(_inbound(request, await request.body()), results)
            return Response(content, media_type=media_type)
        if results is not None:
            body = [
                {"reply": r.reply, "session": r.session_id, "status": r.reason} for r in results
            ]
            return Response(
                json.dumps({"replies": body}, ensure_ascii=False), media_type="application/json"
            )
        if adapter.config.type == "gateway":  # an empty TwiML answer: we reply asynchronously
            return Response("<Response/>", media_type="application/xml")
        return Response("{}", media_type="application/json")

    def web_channel(name: str | None) -> tuple[str, WebChannel]:
        webs = {n: a for n, a in headless.adapters.items() if isinstance(a, WebChannel)}
        if name is None and webs:
            name = next(iter(webs))
        if name is None or name not in webs:
            raise HTTPException(404, "no web chat here")
        return name, webs[name]

    @app.get("/")
    async def landing() -> Response:
        name, adapter = web_channel(None)
        page = render_landing(current().spec, current().resolved.data, name,
                              public=adapter.public)  # fmt: skip
        return Response(page, media_type="text/html; charset=utf-8")

    @app.get("/chat")
    @app.get("/chat/{name}")
    async def chat_page(name: str | None = None) -> Response:
        name, adapter = web_channel(name)
        spec = current().spec
        values = current().resolved.data.get("values") or {}
        title = (values.get("business_name") or (spec.tenant.name if spec.tenant else None)
                 or spec.solution.name or "Chat")  # fmt: skip
        brand = spec.branding
        page = render_chat(
            name, str(title), locale=spec.solution.locale, public=adapter.public,
            color=brand.colors.primary if brand and brand.colors else None,
            logo=brand.logo if brand else None, icon=favicon(spec),
        )  # fmt: skip
        return Response(page, media_type="text/html; charset=utf-8")

    @app.get("/channels/{name}/outbox")
    async def chat_outbox(name: str, contact: str, request: Request) -> dict[str, Any]:
        name, adapter = web_channel(name)
        try:
            return {"messages": adapter.poll(_inbound(request, b""), contact)}
        except Unauthorized as exc:
            raise HTTPException(401, str(exc)) from None
        except ChannelError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/hooks/{path:path}", status_code=202)
    async def hook(path: str, request: Request) -> dict[str, Any]:
        found = headless.webhook(path)
        if found is None:
            raise HTTPException(404, "no webhook trigger at this path")
        name, _ = found
        inbound = _inbound(request, await request.body())
        try:
            headless.verify_webhook(name, inbound)
        except Unauthorized as exc:
            raise HTTPException(401, str(exc)) from None
        event = inbound.json() if inbound.body else {}
        delivery = (
            inbound.headers.get("x-github-delivery")
            or inbound.headers.get("x-delivery-id")
            or inbound.headers.get("x-request-id")
        )
        job = await headless.fire(
            name, event if isinstance(event, dict) else {"body": event}, delivery
        )
        return {"queued": job is not None, "duplicate": job is None}

    # --- admin -------------------------------------------------------------------------

    @app.get("/admin/inbox", dependencies=[Depends(admin)])
    async def inbox(status: str | None = "open", kind: str | None = None) -> list[dict[str, Any]]:
        items = await current().inbox.list(status or None, kind)
        reveal = current().pii.policy.classes
        return [
            {
                "id": i.id,
                "kind": i.kind,
                "status": i.status,
                "title": i.title,
                "session": i.session_id,
                "payload": await current().pii.detokenize_obj(i.payload, reveal),
                "created_at": i.created_at,
                "decided_by": i.decided_by,
                "note": i.note,
            }
            for i in items
        ]

    @app.post("/admin/inbox/{item_id}/decision", dependencies=[Depends(admin)])
    async def decide(item_id: str, decision: Decision) -> dict[str, Any]:
        try:
            item = await headless.decide(
                item_id, decision.approved, decision.by, decision.note, decision.text
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"id": item.id, "status": item.status}

    async def recent_rows(limit: int) -> list[dict[str, Any]]:
        """The latest conversations with their texts (PII shown, as for one session)."""
        inst = current()
        if not hasattr(inst.store, "recent"):
            raise HTTPException(501, "this store cannot list recent conversations")
        reveal = inst.pii.policy.classes
        out = []
        for row in await inst.store.recent(scope, limit):
            loaded = await inst.store.load(scope, row["session_id"])
            messages = [
                {"role": str(m.role),
                 "text": await inst.pii.detokenize(m.text(), reveal, mask=False)}
                for m in loaded.messages if m.text()
            ]  # fmt: skip
            if any(m["role"] == "user" for m in messages):
                out.append({"id": row["session_id"], "channel": row["channel"],
                            "last_active": row["last_active"], "messages": messages})  # fmt: skip
        return out

    @app.get("/admin/sessions", dependencies=[Depends(admin)])
    async def sessions(limit: int = 20) -> list[dict[str, Any]]:
        """The latest real conversations, newest first, with their texts (what ``replay``
        runs again on a rebuilt client)."""
        return await recent_rows(max(1, min(limit, 200)))

    @app.get("/admin/sessions/{session_id}", dependencies=[Depends(admin)])
    async def session(session_id: str) -> dict[str, Any]:
        try:
            loaded = await current().store.load(scope, session_id)
        except FileNotFoundError:
            raise HTTPException(404, "no such session") from None
        reveal = current().pii.policy.classes
        return {
            "id": loaded.id,
            "agent": loaded.agent_id,
            "state": await current().store.state(scope, session_id),
            "messages": [
                {
                    "role": str(m.role),
                    "text": await current().pii.detokenize(m.text(), reveal, mask=False),
                }
                for m in loaded.messages
                if m.text()
            ],
        }

    @app.post("/admin/sessions/{session_id}/reply", dependencies=[Depends(admin)])
    async def reply(session_id: str, body: OperatorReply) -> dict[str, str]:
        try:
            await headless.operator_reply(session_id, body.text, body.by)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from None
        return {"status": "sent"}

    @app.post("/admin/consent", dependencies=[Depends(admin)])
    async def consent(change: ConsentChange) -> dict[str, str]:
        await current().consent.set(
            scope, change.contact, change.channel, change.status, change.source
        )
        await current().audit.record(
            scope, f"admin:{change.source}", f"consent_{change.status}", change.channel, {}
        )
        return {"status": change.status}

    @app.patch("/admin/contacts/{contact_key}", dependencies=[Depends(admin)])
    async def contact(contact_key: str, attrs: dict[str, Any]) -> dict[str, Any]:
        """Set contact attributes that conditions read (``contact.verified``...); null
        removes one. Set by the client's systems, never by the model."""
        merged = await current().contacts.update(scope, contact_key, attrs)
        await current().audit.record(
            scope, "admin", "contact_updated", "contact", {"keys": sorted(attrs)}
        )
        return merged

    @app.get("/admin/contacts/{contact_key}/memory", dependencies=[Depends(admin)])
    async def contact_memory(contact_key: str) -> list[dict[str, Any]]:
        from ..memory.store import MemoryScope

        found = await current().memory.active(MemoryScope("contact", contact_key))
        reveal = current().pii.policy.classes
        return [
            {"id": m.id, "layer": m.layer, "key": m.key, "version": m.version,
             "content": await current().pii.detokenize(m.content, reveal, mask=False)}
            for m in found
        ]  # fmt: skip

    @app.delete("/admin/contacts/{contact_key}/memory", dependencies=[Depends(admin)])
    async def forget_contact(contact_key: str) -> dict[str, int]:
        """The contact's right to be forgotten: everything remembered about them goes."""
        from ..memory.store import MemoryScope

        removed = await current().memory.forget(MemoryScope("contact", contact_key))
        await current().audit.record(
            scope, "admin", "memory_forgotten", "contact", {"removed": removed}
        )
        return {"removed": removed}

    @app.get("/admin/audit/verify", dependencies=[Depends(admin)])
    async def audit_verify() -> dict[str, Any]:
        broken = await current().audit.verify(scope)
        return {"intact": broken is None, "first_broken_seq": broken}

    @app.get("/admin/spend", dependencies=[Depends(admin)])
    async def spend(day: str | None = None) -> dict[str, float]:
        return await current().spend.day(scope, day)

    @app.get("/admin/costs", dependencies=[Depends(admin)])
    async def costs(
        since: str | None = None, until: str | None = None, by: str = "vendor"
    ) -> dict[str, Any]:
        """Model spend at vendor list prices (no markup), grouped by any of day, agent, role,
        vendor and model (comma-separated)."""
        try:
            return await current().usage.report(
                scope, since=since, until=until, by=[b.strip() for b in by.split(",") if b]
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/admin/metrics", dependencies=[Depends(admin)])
    async def metrics(since: str = "7d") -> dict[str, Any]:
        """Quality metrics: tool success, verify pass, escalation rate, cost per resolved."""
        try:
            return await quality(current().db, scope, since)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/admin/config", dependencies=[Depends(admin)])
    async def config() -> dict[str, Any]:
        store = ConfigStore(current().db, scope)
        active = await store.active()
        return {
            "running": current().resolved.version_hash,
            "active": active.version if active else None,
            "versions": [
                {"version": v.version, "hash": v.hash, "status": v.status, "by": v.created_by,
                 "note": v.note, "created_at": v.created_at}
                for v in await store.history()
            ],
        }  # fmt: skip

    @app.post("/admin/config/{version}/activate", dependencies=[Depends(admin)])
    async def activate(version: int, by: str = "operator") -> dict[str, Any]:
        store = ConfigStore(current().db, scope)
        try:
            await store.activate(version)
        except ConfigError as exc:
            raise HTTPException(409, str(exc)) from None
        await current().audit.record(scope, by, "config_activated", f"config/v{version}", {})
        await apply_active(headless)
        return {"active": version, "running": current().resolved.version_hash}

    @app.post("/admin/config/rollback", dependencies=[Depends(admin)])
    async def rollback(by: str = "operator") -> dict[str, Any]:
        store = ConfigStore(current().db, scope)
        try:
            version = await store.rollback()
        except ConfigError as exc:
            raise HTTPException(409, str(exc)) from None
        await current().audit.record(scope, by, "config_rollback", f"config/v{version.version}", {})
        await apply_active(headless)
        return {"active": version.version, "running": current().resolved.version_hash}

    @app.post("/admin/events", dependencies=[Depends(admin)])
    async def event(body: dict[str, Any]) -> dict[str, Any]:
        """An event from the client's systems (``{"name": ..., "data": {...}}``): resumes
        workflow runs waiting for it and fires event triggers."""
        name = body.get("name")
        if not isinstance(name, str) or not name:
            raise HTTPException(400, "an event needs a name")
        woken = await headless.emit(name, dict(body.get("data") or {}))
        return {"woken": woken}

    @app.post("/admin/triggers/{name}/run", dependencies=[Depends(admin)])
    async def run_trigger(name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a file trigger's scan now, or a batch trigger now (``{"items": [...]}`` pushes
        the items instead of calling its source tool)."""
        trig = headless.triggers.get(name)
        if trig is None or trig.type not in ("file", "batch"):
            raise HTTPException(404, "no file or batch trigger by that name")
        if trig.type == "file":
            return {"queued": await headless.scan_files(name)}
        given = (body or {}).get("items")
        if given is not None and not isinstance(given, list):
            raise HTTPException(400, "items must be a list")
        try:
            return await headless.run_batch(name, given)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/admin/sources/{source}/items", dependencies=[Depends(admin)])
    async def items(source: str, body: dict[str, Any]) -> dict[str, Any]:
        """Items a relative trigger watches (``{"items": [{"id", "start", ...}],
        "replace": bool}``), e.g. appointments pushed by the client's calendar."""
        given = body.get("items")
        if not isinstance(given, list) or not all(
            isinstance(i, dict) and "id" in i and "start" in i for i in given
        ):
            raise HTTPException(400, "items need an id and a start")
        count = await headless.upsert_items(source, given, replace=bool(body.get("replace")))
        return {"items": count}

    # --- feedback -----------------------------------------------------------------------

    @app.post("/admin/sessions/{session_id}/feedback", dependencies=[Depends(admin)])
    async def feedback(session_id: str, body: Feedback) -> dict[str, Any]:
        """A thumbs up or down on a conversation; a down with a comment proposes a rule."""
        try:
            proposed = await headless.feedback(session_id, body.rating, body.comment, body.by)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"proposed": proposed}

    @app.get("/admin/constraints", dependencies=[Depends(admin)])
    async def constraints(status: str | None = None) -> list[dict[str, Any]]:
        return [c.public() for c in await current().constraints.all(status)]

    @app.post("/admin/constraints", dependencies=[Depends(admin)])
    async def add_constraint(body: NewConstraint) -> dict[str, Any]:
        """A rule a person writes directly: pinned at once."""
        if body.agent != "*" and body.agent not in current().spec.agents:
            raise HTTPException(404, f"unknown agent {body.agent!r}")
        if not body.text.strip():
            raise HTTPException(400, "a rule needs text")
        added = await current().constraints.add(body.agent, body.text.strip(), body.by)
        await current().audit.record(
            scope, body.by, "constraint_added", f"constraint/{added.id}", {"text": added.text}
        )
        return added.public()

    @app.delete("/admin/constraints/{constraint_id}", dependencies=[Depends(admin)])
    async def retire_constraint(constraint_id: str, by: str = "operator") -> dict[str, Any]:
        found = await current().constraints.get(constraint_id)
        if found is None:
            raise HTTPException(404, f"no constraint {constraint_id!r}")
        await current().constraints.decide(constraint_id, "retired", by)
        await current().audit.record(
            scope, by, "constraint_retired", f"constraint/{constraint_id}", {}
        )
        return {"id": constraint_id, "status": "retired"}

    # --- knowledge ----------------------------------------------------------------------

    def corpus_of(name: str) -> Any:
        kb = current().knowledge
        if name not in kb.corpora:
            raise HTTPException(404, f"unknown corpus {name!r}")
        return kb

    @app.get("/admin/knowledge", dependencies=[Depends(admin)])
    async def knowledge_corpora() -> dict[str, list[str]]:
        return {"corpora": sorted(current().spec.knowledge.corpora)}

    @app.get("/admin/knowledge/gaps", dependencies=[Depends(admin)])
    async def knowledge_gaps(status: str = "open") -> list[dict[str, Any]]:
        """Questions the documents did not answer, most asked first (``status``: open,
        answered, dismissed or all)."""
        if status not in (*GAP_STATUSES, "all"):
            raise HTTPException(400, f"status must be one of {', '.join(GAP_STATUSES)}, all")
        return await current().knowledge.gaps(None if status == "all" else status)

    @app.post("/admin/knowledge/gaps", dependencies=[Depends(admin)])
    async def knowledge_gap_set(body: dict[str, Any]) -> dict[str, Any]:
        """Mark a gap (``{"id", "status"}``): answered once the FAQ covers it, dismissed
        when it is not something the assistant should answer."""
        gap_id, status = body.get("id"), body.get("status")
        if not isinstance(gap_id, str) or status not in GAP_STATUSES:
            raise HTTPException(400, f"a gap needs an id and a status ({', '.join(GAP_STATUSES)})")
        if not await current().knowledge.set_gap(gap_id, str(status)):
            raise HTTPException(404, "no such gap")
        await current().audit.record(
            scope, "admin", "knowledge_gap", f"gap/{gap_id}", {"status": status}
        )
        return {"id": gap_id, "status": status}

    @app.get("/admin/knowledge/{corpus}/document", dependencies=[Depends(admin)])
    async def knowledge_document(corpus: str, uri: str) -> dict[str, Any]:
        doc: dict[str, Any] | None = await corpus_of(corpus).document(corpus, uri)
        if doc is None:
            raise HTTPException(404, "no such document")
        return doc

    @app.get("/admin/knowledge/{corpus}/documents", dependencies=[Depends(admin)])
    async def knowledge_documents(corpus: str) -> list[dict[str, Any]]:
        return list(await corpus_of(corpus).documents(corpus))

    @app.put("/admin/knowledge/{corpus}/documents", dependencies=[Depends(admin)])
    async def knowledge_put(corpus: str, body: dict[str, Any]) -> dict[str, Any]:
        """Add or replace a document (``{"uri", "text", "title"?, "format"?, "owner"?}``): how
        sources synced elsewhere (Drive, S3, a CMS) reach the index. Formats: markdown, text,
        html. ``"owner": true`` is the business owner's edit of a document (its FAQ): it stays
        over the source file's text until that file changes."""
        kb = corpus_of(corpus)
        uri, text, fmt = body.get("uri"), body.get("text"), body.get("format", "markdown")
        if not isinstance(uri, str) or not uri or not isinstance(text, str):
            raise HTTPException(400, "a document needs a uri and text")
        if fmt not in ("markdown", "text", "html"):
            raise HTTPException(400, "format must be markdown, text or html")
        title = body.get("title") if isinstance(body.get("title"), str) else None
        if body.get("owner") is True:  # the business owner's edit: kept over the file's text
            doc_id, outcome = await kb.override(corpus, uri, text)
        else:
            doc_id, outcome = await kb.put(corpus, uri, text, fmt=fmt, title=title)
        await current().audit.record(
            scope, "admin", f"knowledge_{outcome}", f"knowledge/{corpus}", {"uri": uri}
        )
        return {"id": doc_id, "result": outcome}

    @app.post("/admin/knowledge/{corpus}/check", dependencies=[Depends(admin)])
    async def knowledge_check(corpus: str, body: dict[str, Any]) -> dict[str, Any]:
        """Before an owner's edit goes live: the latest real conversations, again, on a
        throwaway copy of this instance with the proposed text (``{"uri", "text",
        "limit"?}``), each reply judged against the one the customer got."""
        import tempfile

        from ..constructor.evals import RecordingApprover
        from ..constructor.replay import conversations, counts, replay, summary

        inst = current()
        corpus_of(corpus)
        uri, text = body.get("uri"), body.get("text")
        if not isinstance(uri, str) or not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "a check needs the document's uri and its new text")
        if "verifier" not in (inst.spec.models.roles if inst.spec.models else {}):
            raise HTTPException(501, "this solution has no verifier model role to judge with")
        limit = max(1, min(int(body.get("limit") or 10), 50))
        rows = await recent_rows(limit)
        convos = conversations(rows)

        async def open_copy(state: Path) -> Instance:
            options = dataclasses.replace(
                inst.options, state_root=state, database=None, database_url=None,
                approver=RecordingApprover(), telemetry=None,
            )  # fmt: skip
            copy = await Instance.open(inst.resolved, options)
            await copy.knowledge.override(corpus, uri, text)  # the proposed edit, only here
            return copy

        with tempfile.TemporaryDirectory(prefix="dif-check-") as work:
            done = await replay(open_copy, convos, Path(work)) if convos else []
        worse = [{"customer": t.customer, "before": t.before, "after": t.after, "why": t.why}
                 for c in done for t in c.turns if t.verdict == "worse"]  # fmt: skip
        await inst.audit.record(scope, "admin", "knowledge_check", f"knowledge/{corpus}",
                                {"uri": uri, "counts": counts(done)})  # fmt: skip
        return {"summary": summary(done) if done else "no real conversations yet",
                "counts": counts(done), "worse": worse}  # fmt: skip

    @app.delete("/admin/knowledge/{corpus}/documents", dependencies=[Depends(admin)])
    async def knowledge_delete(corpus: str, uri: str) -> dict[str, bool]:
        removed = await corpus_of(corpus).delete(corpus, uri)
        if removed:
            await current().audit.record(
                scope, "admin", "knowledge_deleted", f"knowledge/{corpus}", {"uri": uri}
            )
        return {"removed": removed}

    @app.post("/admin/knowledge/{corpus}/sync", dependencies=[Depends(admin)])
    async def knowledge_sync(corpus: str) -> dict[str, Any]:
        corpus_of(corpus)
        report = await current().sync_knowledge(corpus)
        return dataclasses.asdict(report)

    @app.get("/admin/knowledge/{corpus}/search", dependencies=[Depends(admin)])
    async def knowledge_search(corpus: str, q: str) -> list[dict[str, Any]]:
        """What an agent would get for ``q`` (for tuning ``min_score``); scores below the
        threshold are included, marked ``below_min_score``."""
        kb = corpus_of(corpus)
        floor = float(kb.retrieval(corpus).get("min_score") or 0.0)
        hits = await kb.search(corpus, q, min_score=0.0)
        return [
            {"id": h.id, "document": h.title, "section": h.section, "score": h.score,
             "below_min_score": h.score < floor, "text": h.text}
            for h in hits
        ]  # fmt: skip

    @app.post("/admin/agents/{name}/tasks", dependencies=[Depends(admin)])
    async def agent_task(name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Give an agent a task outside any conversation (``{"text": ...}``)."""
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "a task needs text")
        try:
            job = await headless.fire_agent(name, text)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"queued": job is not None}

    @app.get("/admin/runs", dependencies=[Depends(admin)])
    async def runs(workflow: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return [
            {"id": r.id, "workflow": r.workflow, "status": r.status, "outcome": r.outcome,
             "error": r.error, "step": r.state.get("waiting_step")}
            for r in await headless.engine.runs(workflow, status)
        ]  # fmt: skip

    @app.get("/admin/runs/{run_id}", dependencies=[Depends(admin)])
    async def run(run_id: str) -> dict[str, Any]:
        found = await headless.engine.get(run_id)
        if found is None:
            raise HTTPException(404, "no such run")
        return {"id": found.id, "workflow": found.workflow, "status": found.status,
                "outcome": found.outcome, "error": found.error, "input": found.input,
                "steps": found.steps}  # fmt: skip

    @app.get("/admin/jobs", dependencies=[Depends(admin)])
    async def jobs() -> dict[str, int]:
        return await headless.queue.counts(scope)

    return app
