"""``dif-general-harness admin``: running a live instance day to day, from its server."""

from __future__ import annotations

import argparse
import io
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.cli import admin_command
from dif_general_harness.core.messages import Message
from dif_general_harness.service.admin_client import Admin, AdminError
from tests.support import ADMIN_H, API_TOKEN, Env, calls

FAQ = "# Roberta\n\n## Prices\nAverage price: 2,000 MXN.\n\n## Payment\nDebit and cash.\n"


def _faq_spec(docs: Path) -> Any:
    def edit(spec: dict[str, Any]) -> None:
        spec["knowledge"] = {"corpora": {"faq": {
            "sources": [{"type": "file", "path": str(docs)}],
            "retrieval": {"read_whole_below": 20000, "cite": False},
        }}}  # fmt: skip
        spec["agents"]["front"]["knowledge"] = ["faq"]

    return edit


def _args(command: str, **extra: Any) -> argparse.Namespace:
    return argparse.Namespace(command=command, url=None, token_file=None, **extra)


async def test_the_owner_edits_the_faq_and_it_stays(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "faq.md").write_text(FAQ)
    env = Env(tmp_path, [], edit=_faq_spec(docs))
    inst, _, client = await env.open()
    client.headers.update(ADMIN_H)
    admin = Admin(client)
    async with inst, client:
        assert (await admin.faq_show()).startswith("# Roberta")
        edited = FAQ.replace("2,000 MXN", "1,800 MXN").replace("Debit and cash.", "Debit, cash.")
        assert "Saved" in await admin.faq_set(edited)
        assert "No change" in await admin.faq_set(edited)
        shown = await admin.faq_show()
        assert "1,800 MXN" in shown and "(edited by the owner)" in shown

        assert (await client.post("/admin/knowledge/faq/sync")).status_code == 200
        assert "1,800 MXN" in await admin.faq_show()  # a restart's sync keeps the owner's edit

        (docs / "faq.md").write_text(FAQ.replace("2,000 MXN", "2,100 MXN"))  # a new release
        await client.post("/admin/knowledge/faq/sync")
        shown = await admin.faq_show()
        assert "2,100 MXN" in shown and "owner" not in shown  # the new release wins

        with pytest.raises(AdminError, match="empty"):
            await admin.faq_set("  ")


async def test_inbox_conversation_reply_costs_and_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = [
        calls(("h1", "handoff.human", {"reason": "wants to talk to Roberta"})),
        Message.assistant("Te comunico con Roberta."),
    ]
    env = Env(tmp_path, script)
    inst, _, client = await env.open()
    async with inst, client:
        r = await client.post(
            "/channels/api", json={"contact": "ana@x.mx", "text": "Quiero hablar con Roberta"},
            headers={"authorization": f"Bearer {API_TOKEN}"},
        )  # fmt: skip
        session = r.json()["replies"][0]["session"]
        client.headers.update(ADMIN_H)
        admin = Admin(client)
        status = await admin.status()
        assert "Online: acme-desk" in status and "Waiting for a person: 1" in status
        inbox = await admin.inbox()
        assert "wants to talk to Roberta" in inbox and session in inbox
        shown = await admin.show(session)
        assert "Customer: Quiero hablar con Roberta" in shown
        assert "Assistant: Te comunico con Roberta." in shown
        assert "Sent" in await admin.reply(session, "Hola Ana, soy Roberta.", "roberta")
        assert "Hola Ana, soy Roberta." in await admin.show(session)
        assert "Total: $" in await admin.costs()

        assert await admin_command(_args("status"), client=client) == 0
        assert "Online: acme-desk" in capsys.readouterr().out
        args = _args("faq", action="set", file=io.StringIO("x"), corpus=None, uri=None)
        assert await admin_command(args, client=client) == 1  # no FAQ: an error, no traceback
        assert "error: this instance has no FAQ" in capsys.readouterr().err
        client.headers["authorization"] = "Bearer wrong"
        assert await admin_command(_args("inbox"), client=client) == 1
        assert "admin token was refused" in capsys.readouterr().err
