"""Questions the documents did not answer become the owner's list of what the FAQ lacks:
recorded with the contact's own words, counted when asked again, marked answered or
dismissed, reopened when an "answered" one is asked again, and purged with conversations."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.cli import admin_command
from dif_general_harness.core.messages import Message
from dif_general_harness.governance.retention import purge
from dif_general_harness.service.admin_client import Admin
from tests.support import ADMIN_H, Env, calls
from tests.test_knowledge import _ask, _docs, _kb_spec

PARKING = ("k1", "knowledge.search_faq", {"query": "parking for bicycles"})


def _not_found(times: int) -> list[Any]:
    return [m for _ in range(times) for m in (calls(PARKING), Message.assistant("No lo sé."))]


async def test_unanswered_questions_become_the_owners_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = Env(tmp_path, _not_found(4), edit=_kb_spec(_docs(tmp_path)))
    inst, _, client = await env.open()
    async with inst, client:
        await _ask(client, "¿Tienen estacionamiento para bicicletas?")
        await _ask(client, "tienen estacionamiento para bicicletas")  # the same, other spelling
        [gap] = await inst.knowledge.gaps()
        assert gap["question"] == "tienen estacionamiento para bicicletas"  # latest wording
        assert gap["asked"] == 2 and gap["corpus"] == "faq" and gap["status"] == "open"

        client.headers.update(ADMIN_H)
        admin = Admin(client)
        listed = await admin.faq_gaps()
        assert f"{gap['id']}  x2" in listed and "admin faq done ID" in listed
        assert "marked answered" in await admin.faq_mark(gap["id"], "answered")
        assert await inst.knowledge.gaps() == []

        await _ask(client, "¿Tienen estacionamiento para bicicletas?")  # the FAQ still lacks it
        [again] = await inst.knowledge.gaps()
        assert again["id"] == gap["id"] and again["asked"] == 3 and again["status"] == "open"

        args = argparse.Namespace(command="faq", action="dismiss", target=gap["id"],
                                  url=None, token_file=None, corpus=None, uri=None)  # fmt: skip
        assert await admin_command(args, client=client) == 0
        await _ask(client, "¿Tienen estacionamiento para bicicletas?")
        assert await inst.knowledge.gaps() == []  # a dismissed question stays out
        [kept] = await inst.knowledge.gaps(None)
        assert kept["status"] == "dismissed" and kept["asked"] == 4
        assert await inst.audit.records(inst.scope, action="knowledge_gap")
        capsys.readouterr()

        r = await client.get("/admin/knowledge/gaps", params={"status": "nope"})
        assert r.status_code == 400
        r = await client.post("/admin/knowledge/gaps", json={"id": "zzz", "status": "answered"})
        assert r.status_code == 404

        removed = await purge(inst.db, inst.scope, {"conversations": "1d"},
                              now=time.time() + 3 * 86400)  # fmt: skip
        assert removed["knowledge_gaps"] == 1 and await inst.knowledge.gaps(None) == []


async def test_an_answer_with_no_source_left_is_a_gap_too(tmp_path: Path) -> None:
    invented = Message.assistant("We accept bitcoin and gold bars [kb:zzzzzzzzzz].")
    search = calls(("k1", "knowledge.search_faq", {"query": "payment methods"}))
    env = Env(tmp_path, [search, invented, invented], edit=_kb_spec(_docs(tmp_path)))
    inst, _, client = await env.open()
    async with inst, client:
        reply = await _ask(client, "¿Aceptan bitcoin?")
        assert reply == "I could not find a sourced answer to that in our documents."
        [gap] = await inst.knowledge.gaps()
        assert gap["question"] == "¿Aceptan bitcoin?"  # the contact's words, not the rewrite note
