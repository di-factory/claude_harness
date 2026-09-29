"""Shared fixtures for service-level tests: a small pack with every channel kind, a fake
provider, mocked provider APIs and a controllable queue clock."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx2

from dif_general_harness.channels.gateway import signature
from dif_general_harness.core.messages import Message, Role, ToolUseBlock
from dif_general_harness.providers import FakeProvider
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.service import Headless, create_app
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tenancy import EnvSecrets

TWILIO_SID, TWILIO_TOKEN = "AC" + "1" * 32, "twilio-auth-token"
TG_TOKEN = "123456:tg-bot-token"
API_TOKEN, ADMIN, HOOK = "api-token-123", "admin-token-456", "crm-hook-secret"
PUBLIC = "https://desk.example.com"
ANA = "+5215512345678"


def desk_pack(
    root: Path, edit: Any = None, tenant: str = "acme", instance_id: str = "acme-desk"
) -> Path:
    """The pack (edited by ``edit(spec)`` if given) and one instance of it."""
    pack = root / "packs" / "desk"
    if (pack / "pack.json").exists():  # a second instance of the same pack
        return _desk_instance(root, tenant, instance_id)
    (pack / "prompts").mkdir(parents=True)
    (pack / "evals").mkdir()
    (pack / "evals" / "smoke.yaml").write_text("id: smoke\nturns: []\n")
    (pack / "prompts" / "front.md").write_text("You answer for {{var.business}}.")
    (pack / "prompts" / "ops.md").write_text("You watch operations.")
    spec = {
        "spec_version": "1",
        "kind": "pack",
        "solution": {"id": "desk", "version": "1.0.0", "lob": "pyme"},
        "variables": {"business": {"type": "string", "required": True}},
        "secrets": {
            n: {"description": n} for n in ["llm", "twilio", "api_token", "telegram", "crm_hook"]
        },
        "models": {
            "roles": {"main": {"provider": "anthropic", "model": "claude-opus-5-5"}},
            "providers": {"anthropic": {"api_key": {"$secret": "llm"}}},
        },
        "agents": {
            "front": {
                "prompt": "prompts/front.md",
                "model_role": "main",
                "tools": ["notes.*"],
                "handoffs": ["human"],
            },
            "ops": {"prompt": "prompts/ops.md", "model_role": "main", "tools": ["notes.*"]},
        },
        "tools": {"packs": ["general"]},
        "channels": {
            "whatsapp": {
                "type": "gateway",
                "provider": "twilio",
                "credentials": {"$secret": "twilio"},
                "address": "+15550001111",
                "entry_agent": "front",
                "contact_key": "phone",
                "session_window": "24h",
            },
            "api": {
                "type": "api",
                "credentials": {"$secret": "api_token"},
                "entry_agent": "front",
                "contact_key": "email",
            },
            "staff": {
                "type": "telegram",
                "credentials": {"$secret": "telegram"},
                "address": "999",
                "purpose": "hitl",
            },
            "mail": {"type": "email", "purpose": "outbound"},
        },
        "triggers": {
            "morning": {
                "type": "schedule",
                "cron": "0 9 * * *",
                "agent": "ops",
                "input": "Morning check.",
            },
            "lead": {
                "type": "webhook",
                "path": "/hooks/crm",
                "auth": {"$secret": "crm_hook"},
                "agent": "ops",
                "input": "New lead {{event.lead.name}} from {{event.lead.source}}",
            },
            "report": {"type": "schedule", "cron": "0 19 * * *", "workflow": "report"},
        },
        "workflows": {"report": {"steps": [{"id": "done", "type": "end"}]}},
        "governance": {
            "pii": {"classes": ["name", "phone"], "tokenize": True, "reveal_in_output": ["name"]},
            "consent": {"required": True, "channels": ["whatsapp"], "opt_out_keywords": ["BAJA"]},
            "audit": {"level": "full"},
        },
        "hitl": {
            "notify": [{"channel": "staff"}],
            "approval_timeout": "2h",
            "on_timeout": "reject",
        },
        "evals": {"suites": ["evals/smoke.yaml"]},
    }
    if edit is not None:
        edit(spec)
    (pack / "pack.json").write_text(json.dumps(spec))
    return _desk_instance(root, tenant, instance_id)


def _desk_instance(root: Path, tenant: str, instance_id: str) -> Path:
    inst = root / "instances" / f"{instance_id}.json"
    inst.parent.mkdir(exist_ok=True)
    inst.write_text(
        json.dumps(
            {
                "spec_version": "1",
                "kind": "instance",
                "solution": {"id": instance_id, "version": "1.0.0", "lob": "pyme"},
                "extends": ["desk@^1.0"],
                "tenant": {"id": tenant, "name": tenant.upper(), "timezone": "America/Mexico_City"},
                "values": {"business": f"{tenant.upper()} Dental"},
            }
        )
    )
    return inst


class Clock:
    def __init__(self) -> None:
        self.now = 1_790_000_000.0  # 2026-09-21

    def __call__(self) -> float:
        return self.now


class Env:
    def __init__(
        self,
        tmp_path: Path,
        script: list[Any],
        edit: Any = None,
        secrets: dict[str, str] | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.edit = edit
        self.secrets = secrets or {}
        self.provider = FakeProvider(script)
        self.sent: list[httpx2.Request] = []
        self.clock = Clock()

    def outbound(self) -> httpx2.AsyncClient:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.sent.append(request)
            if "telegram" in request.url.host:
                return httpx2.Response(200, json={"ok": True, "result": {"message_id": 7}})
            if "slack.com" in request.url.host:
                return httpx2.Response(200, json={"ok": True, "ts": "1700000000.000200"})
            return httpx2.Response(201, json={"sid": f"SM{len(self.sent)}"})

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))

    async def open(
        self, *, tenant: str = "acme", instance_id: str = "acme-desk", database: Any = None
    ) -> tuple[Instance, Headless, httpx2.AsyncClient]:
        instance_file = desk_pack(self.tmp_path, self.edit, tenant, instance_id)
        resolved = load_instance(instance_file, PackCatalog(roots=[self.tmp_path / "packs"]))
        assert resolved.ok, resolved.issues
        secrets = EnvSecrets(
            {
                "DIF_SECRET_TWILIO": f"{TWILIO_SID}:{TWILIO_TOKEN}",
                "DIF_SECRET_API_TOKEN": API_TOKEN,
                "DIF_SECRET_TELEGRAM": TG_TOKEN,
                "DIF_SECRET_CRM_HOOK": HOOK,
                **{f"DIF_SECRET_{k.upper()}": v for k, v in self.secrets.items()},
            }
        )
        options = RuntimeOptions(
            state_root=self.tmp_path / "state",
            secrets=secrets,
            provider=self.provider,
            database=database,
        )
        inst = await Instance.open(resolved, options)
        headless = await Headless.build(
            inst, http_client=self.outbound(), public_url=PUBLIC, clock=self.clock
        )
        app = create_app(headless, admin_token=ADMIN, run_worker=False)
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://desk.internal"
        )
        return inst, headless, client

    def texts(self, host: str) -> list[dict[str, Any]]:
        out = []
        for r in self.sent:
            if host in r.url.host:
                out.append(
                    json.loads(r.content)
                    if r.headers.get("content-type", "").startswith("application/json")
                    else dict(httpx2.QueryParams(r.content.decode()))
                )
        return out


def whatsapp(
    text: str, sid: str, *, sender: str = ANA, name: str = "Ana"
) -> tuple[str, dict[str, str]]:
    params = {"From": f"whatsapp:{sender}", "Body": text, "MessageSid": sid, "ProfileName": name}
    sig = signature(TWILIO_TOKEN, f"{PUBLIC}/channels/whatsapp", params)
    return urlencode(params), {
        "content-type": "application/x-www-form-urlencoded",
        "x-twilio-signature": sig,
    }


def calls(*calls: tuple[str, str, dict[str, Any]]) -> Message:
    return Message(
        role=Role.ASSISTANT, content=[ToolUseBlock(id=i, name=n, input=a) for i, n, a in calls]
    )


ADMIN_H = {"authorization": f"Bearer {ADMIN}"}
