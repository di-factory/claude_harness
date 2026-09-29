"""The control plane's HTTP API.

Instance agents (bearer: the instance's own token, scoped to its path):
- ``POST /v1/instances/{tenant}/{instance}/heartbeat``: aggregates only;
- ``GET  /v1/instances/{tenant}/{instance}/config?running=<hash>``: the pending signed offer,
  or 204;
- ``POST /v1/instances/{tenant}/{instance}/results``: ``{offer, status, reason, evals}``.

Di-Factory operators (bearer: the admin token):
- ``POST /v1/admin/instances``: register an instance (returns its token once);
- ``GET  /v1/admin/fleet``: health, config, spend, escalations, evals and pending offers;
- ``POST /v1/admin/instances/{tenant}/{instance}/offers``: a remote config change;
- ``GET  /v1/admin/instances/{tenant}/{instance}/offers``;
- ``POST /v1/admin/rollouts``, ``GET /v1/admin/rollouts/{id}``,
  ``POST /v1/admin/rollouts/{id}/rollback``;
- ``GET  /v1/admin/audit``: the control plane's own audit chain (and whether it is intact);
- ``GET  /v1/public-key``: the key instances verify offers with.
"""

from __future__ import annotations

import hmac
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from .plane import CONTROL_SCOPE, ControlError, ControlPlane


class Register(BaseModel):
    tenant: str
    instance: str
    by: str = "operator"


class NewOffer(BaseModel):
    data: dict[str, Any] | None = None
    rollback_to: str | None = None
    approved_by: str
    gate: Literal["evals", "none"] = "evals"
    note: str = ""


class Step(BaseModel):
    tenant: str
    instance: str
    data: dict[str, Any]


class NewRollout(BaseModel):
    name: str
    approved_by: str
    steps: list[Step]
    gate: Literal["evals", "none"] = "evals"


class Result(BaseModel):
    offer: str
    status: Literal["applied", "rejected"]
    reason: str = ""
    evals: dict[str, Any] | None = None
    running_before: str | None = None  # what the instance ran before (a rollback target)


class By(BaseModel):
    approved_by: str


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else ""


def create_control_app(plane: ControlPlane, *, admin_token: str) -> FastAPI:
    if len(admin_token) < 16:
        raise ValueError("the control plane's admin token must be at least 16 characters")
    app = FastAPI(title="dif-general-harness control plane", docs_url=None, redoc_url=None)

    async def admin(request: Request) -> None:
        if not hmac.compare_digest(_bearer(request), admin_token):
            raise HTTPException(401, "admin token required")

    async def agent(tenant: str, instance: str, request: Request) -> None:
        if not await plane.authenticate(tenant, instance, _bearer(request)):
            raise HTTPException(401, "invalid instance token")

    def bad(exc: ControlError) -> HTTPException:
        return HTTPException(400, str(exc))

    @app.get("/v1/public-key")
    async def public_key() -> dict[str, str]:
        return {"public_key": plane.public_key()}

    # --- instance agents -------------------------------------------------------------------

    base = "/v1/instances/{tenant}/{instance}"

    @app.post(f"{base}/heartbeat", dependencies=[Depends(agent)])
    async def heartbeat(tenant: str, instance: str, report: dict[str, Any]) -> dict[str, str]:
        await plane.heartbeat(tenant, instance, report)
        return {"status": "ok"}

    @app.get(f"{base}/config", dependencies=[Depends(agent)], response_model=None)
    async def config(tenant: str, instance: str, running: str = "") -> Any:
        offer = await plane.pending(tenant, instance)
        if offer is None or (offer.kind == "config" and offer.hash == running):
            return Response(status_code=204)
        return offer.wire()

    @app.post(f"{base}/results", dependencies=[Depends(agent)])
    async def results(tenant: str, instance: str, body: Result) -> dict[str, Any]:
        detail = {"reason": body.reason, "evals": body.evals, "running_before": body.running_before}
        try:
            offer = await plane.result(tenant, instance, body.offer, body.status, detail)
        except ControlError as exc:
            raise bad(exc) from None
        return offer.public()

    # --- operators -------------------------------------------------------------------------

    @app.post("/v1/admin/instances", dependencies=[Depends(admin)])
    async def register(body: Register) -> dict[str, str]:
        token = await plane.register(body.tenant, body.instance, body.by)
        return {"tenant": body.tenant, "instance": body.instance, "token": token}

    @app.get("/v1/admin/fleet", dependencies=[Depends(admin)])
    async def fleet() -> list[dict[str, Any]]:
        return await plane.fleet()

    @app.post("/v1/admin/instances/{tenant}/{instance}/offers", dependencies=[Depends(admin)])
    async def offer(tenant: str, instance: str, body: NewOffer) -> dict[str, Any]:
        try:
            if body.rollback_to:
                made = await plane.offer_rollback(
                    tenant, instance, body.rollback_to, body.approved_by, note=body.note
                )
            elif body.data is not None:
                made = await plane.offer_config(
                    tenant, instance, body.data, body.approved_by, gate=body.gate, note=body.note
                )
            else:
                raise ControlError("an offer needs data or rollback_to")
        except ControlError as exc:
            raise bad(exc) from None
        return made.public()

    @app.get("/v1/admin/instances/{tenant}/{instance}/offers", dependencies=[Depends(admin)])
    async def offers(tenant: str, instance: str) -> list[dict[str, Any]]:
        return [o.public() for o in await plane.offers(tenant, instance)]

    @app.post("/v1/admin/rollouts", dependencies=[Depends(admin)])
    async def rollout(body: NewRollout) -> dict[str, Any]:
        try:
            return await plane.start_rollout(
                body.name, body.approved_by, [s.model_dump() for s in body.steps], body.gate
            )
        except ControlError as exc:
            raise bad(exc) from None

    @app.get("/v1/admin/rollouts/{rollout_id}", dependencies=[Depends(admin)])
    async def get_rollout(rollout_id: str) -> dict[str, Any]:
        try:
            return await plane.rollout(rollout_id)
        except ControlError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post("/v1/admin/rollouts/{rollout_id}/rollback", dependencies=[Depends(admin)])
    async def rollback(rollout_id: str, body: By) -> dict[str, Any]:
        try:
            return await plane.rollback(rollout_id, body.approved_by)
        except ControlError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get("/v1/admin/audit", dependencies=[Depends(admin)])
    async def audit() -> dict[str, Any]:
        records = await plane.audit.records(CONTROL_SCOPE)
        broken = await plane.audit.verify(CONTROL_SCOPE)
        return {
            "intact": broken is None,
            "records": [
                {"seq": r.seq, "ts": r.ts, "actor": r.actor, "action": r.action,
                 "subject": r.subject, "data": r.data}
                for r in records
            ],
        }  # fmt: skip

    return app
