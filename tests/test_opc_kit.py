"""The OPC test kit (docs/testing/opc), end to end and offline: the founder talks to the COO on
Telegram (only the founder's chat is answered), a request becomes a ledger task that wakes
the CGO, the CGO's post waits for the founder, who approves it by replying in the chat, and
the post reaches the LinkedIn stand-in. The stand-up merges all three updates; a CTO alert
becomes one task and one page, without the CTO waking itself."""

from __future__ import annotations

import importlib.util
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx2

from dif_general_harness.channels.telegram import webhook_secret
from dif_general_harness.core.messages import Message
from dif_general_harness.providers import FakeProvider
from dif_general_harness.providers.base import ModelRequest
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.service.app import create_app
from dif_general_harness.service.headless import Headless
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tenancy.secrets import EnvSecrets

from .conftest import EXAMPLES
from .support import calls

KIT = EXAMPLES.parent.parent / "testing" / "opc"
BOT, FOUNDER, ADMIN = "123456:test-bot-token", "4242", "admin-opc"

_spec = importlib.util.spec_from_file_location("opc_stand_ins", KIT / "stand_ins.py")
assert _spec and _spec.loader
stand_ins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stand_ins)


class Team:
    """Each agent's turns, as a model would take them; the verifier passes and is recorded."""

    def __init__(self, scripts: dict[str, list[Message]]) -> None:
        self.turns: dict[str, Iterator[Message]] = {k: iter(v) for k, v in scripts.items()}
        self.reviews: list[ModelRequest] = []
        self.asked: list[tuple[str, ModelRequest]] = []

    def __call__(self, request: ModelRequest) -> Message:
        if request.model_role == "verifier":
            self.reviews.append(request)
            return Message.assistant('{"pass": true, "reason": "follows from the task"}')
        if request.model_role in ("memory_extraction", "compaction"):
            return Message.assistant('{"facts": []}')
        titles = (("cgo", "Growth"), ("coo", "Operations"), ("cto", "Technology"))
        who = next(a for a, title in titles if title in request.system)
        self.asked.append((who, request))
        return next(self.turns[who])


async def _open(tmp_path: Path, team: Team) -> tuple[Any, ...]:
    spec = json.loads((KIT / "opc-test.json").read_text())
    spec["values"]["founder_telegram"] = FOUNDER
    (tmp_path / "opc-test.json").write_text(json.dumps(spec))
    world = stand_ins.World(tmp_path / "outbox.json")
    telegram: list[dict[str, Any]] = []

    def network(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.telegram.org":
            body = json.loads(request.content) if request.content else {}
            telegram.append({"method": request.url.path.rsplit("/", 1)[-1], **body})
            return httpx2.Response(200, json={"ok": True, "result": {"message_id": len(telegram)}})
        assert request.headers["authorization"].startswith("Bearer ")
        body = json.loads(request.content or b"{}")
        query = dict(parse_qsl(request.url.query.decode() if isinstance(
            request.url.query, bytes) else str(request.url.query)))  # fmt: skip
        status, out = world.handle(request.method, request.url.path, query, body)
        return httpx2.Response(status, json=out)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(network))
    resolved = load_instance(tmp_path / "opc-test.json", PackCatalog(roots=[EXAMPLES]))
    assert resolved.ok, [str(i) for i in resolved.issues if i.severity == "error"]
    options = RuntimeOptions(
        state_root=tmp_path / "state",
        secrets=EnvSecrets({"DIF_SECRET_TELEGRAM": BOT, "DIF_SECRET_CRM": "c",
                            "DIF_SECRET_LINKEDIN": "l", "DIF_SECRET_CLOUD_READONLY": "m"}),
        provider=FakeProvider([team] * 60),
        mcp_servers={"crm": stand_ins.crm_server(world)},
        http_client=http,
    )  # fmt: skip
    inst = await Instance.open(resolved, options)
    headless = await Headless.build(inst, http_client=http, public_url="https://x.test/opc")
    await headless.start()
    app = create_app(headless, admin_token=ADMIN, run_worker=False)
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://h")
    client.headers["authorization"] = f"Bearer {ADMIN}"
    return inst, headless, client, world, telegram


SENT = iter(range(100, 10_000))  # Telegram never repeats a message id; a repeat is a retry


async def _say(client: Any, headless: Any, text: str, chat: str = FOUNDER) -> None:
    n = next(SENT)
    update = {"update_id": n, "message": {"message_id": n, "text": text,
              "chat": {"id": int(chat), "type": "private"},
              "from": {"id": int(chat), "first_name": "Ana"}}}  # fmt: skip
    r = await client.post(
        "/channels/founder",
        json=update,
        headers={"x-telegram-bot-api-secret-token": webhook_secret(BOT)},
    )
    assert r.status_code == 200, r.text
    await headless.worker().drain()


def _texts(telegram: list[dict[str, Any]]) -> list[str]:
    return [m["text"] for m in telegram if m["method"] == "sendMessage"]


async def test_a_request_becomes_a_post_the_founder_approves_from_the_chat(tmp_path: Path) -> None:
    post = "Nuevo en Agenda Fácil: recordatorios por WhatsApp para tus pacientes."
    team = Team({
        "coo": [calls(("t1", "ledger.create_task", {"title": "Announce WhatsApp reminders",
                                                     "owner": "cgo", "priority": "p2"})),
                Message.assistant("Listo: se lo pasé al CGO.")],
        "cgo": [calls(("p1", "social.publish_post", {"text": post})),
                Message.assistant("The post waits for the founder's approval."),
                Message.assistant("Noted: published.")],
    })  # fmt: skip
    inst, headless, client, world, telegram = await _open(tmp_path, team)
    async with inst, client:
        assert telegram[0]["method"] == "setWebhook"  # registered at start, on the public URL
        assert telegram[0]["url"] == "https://x.test/opc/channels/founder"

        await _say(client, headless, "Hola, soy Juan", chat="999")  # not the founder's chat
        assert team.asked == [] and _texts(telegram) == []

        await _say(client, headless, "Que el CGO anuncie en LinkedIn los recordatorios.")
        assert "Listo: se lo pasé al CGO." in _texts(telegram)
        [notice] = [t for t in _texts(telegram) if "Approval needed" in t]  # once, not twice
        assert "social.publish_post" in notice and "recordatorios por WhatsApp" in notice
        item = notice.split("]")[0].removeprefix("[inbox ").strip()
        assert f"Reply here: approve {item}" in notice
        assert world.outbox == []  # nothing is published before the founder says so
        [review] = team.reviews
        assert "follows from the task the agent was given" in review.messages[0].text()

        await _say(client, headless, f"approve {item} se ve bien")
        assert f"Approved: {item}." in _texts(telegram)
        assert [(o["kind"], o["text"]) for o in world.outbox] == [("linkedin_post", post)]
        assert any("was granted and social.publish_post ran" in t for t in _texts(telegram))
        decided = await inst.inbox.get(item)
        assert decided is not None and decided.decided_by == f"founder:{FOUNDER}"


async def test_the_stand_up_brief_merges_all_three_updates(tmp_path: Path) -> None:
    team = Team({
        "cgo": [Message.assistant("CGO: two posts drafted, one lead to follow up.")],
        "cto": [Message.assistant("CTO: all green, p95 180 ms.")],
        "coo": [Message.assistant("COO: ledger clean, nothing overdue."),
                Message.assistant("Brief: all good. Decision needed: approve the post.")],
    })  # fmt: skip
    inst, headless, client, _, telegram = await _open(tmp_path, team)
    async with inst, client:
        r = await client.post("/admin/triggers/standup/run")
        assert r.status_code == 200, r.text
        await headless.worker().drain()
    brief = next(req for who, req in team.asked if "Merge the three updates" in _text(req))
    assert "CGO: two posts drafted" in _text(brief) and "CTO: all green" in _text(brief)
    assert "Brief: all good. Decision needed: approve the post." in _texts(telegram)


async def test_a_cto_alert_becomes_one_task_and_one_page(tmp_path: Path) -> None:
    team = Team({
        "cto": [calls(("a1", "cloud.get_alerts", {})),
                calls(("a2", "ledger.create_task", {"title": "Investigate p95 latency",
                                                     "owner": "cto", "priority": "p1",
                                                     "needs_founder": True})),
                Message.assistant("Opened a task; the founder is told.")],
    })  # fmt: skip
    inst, headless, client, world, telegram = await _open(tmp_path, team)
    async with inst, client:
        world.handle("POST", "/control/alert", {}, {"name": "p95 latency high"})
        r = await client.post("/admin/triggers/cto-health/run")
        assert r.status_code == 200, r.text
        await headless.worker().drain()
        assert inst.ledger is not None
        [task] = await inst.ledger.list_tasks()
        await inst.ledger.update(task["id"], {"status": "doing"}, "agent:cto")
        await headless.worker().drain()
        pages = [t for t in _texts(telegram) if "Task needs a person" in t]
        escalations = await inst.inbox.list(kind="escalation")
    assert len([who for who, _ in team.asked if who == "cto"]) == 3  # it did not wake itself
    assert len(pages) == 1 and len(escalations) == 1  # one page, not one per update or target


def _text(request: ModelRequest) -> str:
    return json.dumps([m.model_dump(mode="json") for m in request.messages], ensure_ascii=False)


def test_the_kit_instance_is_valid_and_its_founder_chat_is_a_placeholder() -> None:
    resolved = load_instance(KIT / "opc-test.json", PackCatalog(roots=[EXAMPLES]))
    assert resolved.ok
    assert resolved.data["values"]["founder_telegram"] == "YOUR-TELEGRAM-CHAT-ID"
    assert shutil.which("bash") is None or (KIT / "kit.sh").stat().st_mode & 0o111
