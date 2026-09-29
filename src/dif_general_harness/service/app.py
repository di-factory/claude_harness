"""The HTTP service (FastAPI): channel webhooks, trigger webhooks, the admin API.

Routes:
- ``GET /healthz``: liveness and config version; ``GET /readyz``: the database answers.
- ``POST /channels/{name}``: inbound messages. Gateway and Telegram requests are verified
  and queued (acknowledged at once); REST/web channels answer inline.
- ``POST /hooks/{path}``: webhook triggers, verified with their shared secret
  (``X-Hub-Signature-256`` or a bearer token), deduplicated by delivery id.
- ``/admin/*``: the inbox (list, decide), sessions (view, reply as a person), consent,
  audit verification, spend and job counts. Bearer ``admin_token``; without one the admin
  API is off.

The worker (queue lanes) runs inside the same process by default: one container per
instance (ARCHITECTURE §3.20).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from ..channels import ChannelError, Inbound, Unauthorized
from .headless import Headless


class Decision(BaseModel):
    approved: bool
    note: str = ""
    by: str = "operator"


class OperatorReply(BaseModel):
    text: str
    by: str = "operator"


class ConsentChange(BaseModel):
    contact: str
    channel: str
    status: str
    source: str = "admin"


def _inbound(request: Request, body: bytes) -> Inbound:
    return Inbound(
        url=str(request.url),
        headers={k.lower(): v for k, v in request.headers.items()},
        body=body,
    )


def create_app(
    headless: Headless,
    *,
    admin_token: str | None = None,
    run_worker: bool = True,
    worker_lanes: int = 4,
) -> FastAPI:
    inst = headless.instance
    scope = inst.scope

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task: asyncio.Task[None] | None = None
        if run_worker:
            await headless.start()
            task = asyncio.create_task(headless.worker(concurrency=worker_lanes).run(stop))
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task

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
            "config_version": inst.resolved.version_hash,
        }

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        try:
            await inst.db.fetchone("SELECT 1 AS ok")
        except Exception as exc:
            raise HTTPException(503, f"database unavailable: {type(exc).__name__}") from None
        return {"status": "ready"}

    @app.post("/channels/{name}")
    async def channel(name: str, request: Request) -> Response:
        try:
            results = await headless.receive(name, _inbound(request, await request.body()))
        except Unauthorized as exc:
            raise HTTPException(401, str(exc)) from None
        except ChannelError as exc:
            raise HTTPException(400, str(exc)) from None
        if results is not None:
            body = [
                {"reply": r.reply, "session": r.session_id, "status": r.reason} for r in results
            ]
            return Response(
                json.dumps({"replies": body}, ensure_ascii=False), media_type="application/json"
            )
        adapter = headless.adapters[name]
        if adapter.config.type == "gateway":  # an empty TwiML answer: we reply asynchronously
            return Response("<Response/>", media_type="application/xml")
        return Response("{}", media_type="application/json")

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
        items = await inst.inbox.list(status or None, kind)
        reveal = inst.pii.policy.classes
        return [
            {
                "id": i.id,
                "kind": i.kind,
                "status": i.status,
                "title": i.title,
                "session": i.session_id,
                "payload": await inst.pii.detokenize_obj(i.payload, reveal),
                "created_at": i.created_at,
                "decided_by": i.decided_by,
                "note": i.note,
            }
            for i in items
        ]

    @app.post("/admin/inbox/{item_id}/decision", dependencies=[Depends(admin)])
    async def decide(item_id: str, decision: Decision) -> dict[str, Any]:
        try:
            item = await headless.decide(item_id, decision.approved, decision.by, decision.note)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"id": item.id, "status": item.status}

    @app.get("/admin/sessions/{session_id}", dependencies=[Depends(admin)])
    async def session(session_id: str) -> dict[str, Any]:
        try:
            loaded = await inst.store.load(scope, session_id)
        except FileNotFoundError:
            raise HTTPException(404, "no such session") from None
        reveal = inst.pii.policy.classes
        return {
            "id": loaded.id,
            "agent": loaded.agent_id,
            "state": await inst.store.state(scope, session_id),
            "messages": [
                {
                    "role": str(m.role),
                    "text": await inst.pii.detokenize(m.text(), reveal, mask=False),
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
        if change.status not in {"granted", "revoked"}:
            raise HTTPException(400, "status must be granted or revoked")
        await inst.consent.set(scope, change.contact, change.channel, change.status, change.source)  # type: ignore[arg-type]
        await inst.audit.record(
            scope, f"admin:{change.source}", f"consent_{change.status}", change.channel, {}
        )
        return {"status": change.status}

    @app.get("/admin/audit/verify", dependencies=[Depends(admin)])
    async def audit_verify() -> dict[str, Any]:
        broken = await inst.audit.verify(scope)
        return {"intact": broken is None, "first_broken_seq": broken}

    @app.get("/admin/spend", dependencies=[Depends(admin)])
    async def spend(day: str | None = None) -> dict[str, float]:
        return await inst.spend.day(scope, day)

    @app.get("/admin/jobs", dependencies=[Depends(admin)])
    async def jobs() -> dict[str, int]:
        return await headless.queue.counts(scope)

    return app
