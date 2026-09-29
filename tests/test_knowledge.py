"""Knowledge / RAG (M3.6): chunking, sync, scored retrieval, "not found", citations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dif_general_harness.core.messages import Message, ToolResultBlock
from dif_general_harness.core.scope import Scope
from dif_general_harness.governance.pii import find
from dif_general_harness.knowledge import KnowledgeBase, chunk
from dif_general_harness.knowledge.chunk import MAX_CHARS
from dif_general_harness.knowledge.store import chunk_id, terms
from dif_general_harness.providers.base import ModelRequest
from tests.support import ADMIN_H, API_TOKEN, Env, calls

FAQ = """# Sonrisa FAQ

Answers for our patients.

## Payment methods
We accept cash, debit and credit cards. We do not accept checks.

## Location
We are at Av. Reforma 123, Mexico City. Parking is available in the building.

### Opening hours
Monday to Saturday, 9:00 to 19:00.
"""
PRICES = """<html><head><title>Prices</title><style>p {color: red}</style></head><body>
<h1>Prices</h1><h2>Cleaning</h2><p>A dental cleaning costs 800 MXN.</p>
<h2>Whitening</h2><p>Whitening costs 3,500 MXN per session.</p></body></html>"""


def _docs(root: Path) -> Path:
    docs = root / "docs"
    (docs / "more").mkdir(parents=True, exist_ok=True)
    (docs / "faq.md").write_text(FAQ)
    (docs / "more" / "prices.html").write_text(PRICES)
    (docs / "scan.pdf").write_bytes(b"%PDF-1.4 not read here")
    return docs


CORPUS = {"retrieval": {"top_k": 3, "min_score": 0.5, "cite": True, "not_found": "say_so"}}


def _kb_spec(docs: Path, *, not_found: str = "say_so", rules: list[Any] | None = None) -> Any:
    def edit(spec: dict[str, Any]) -> None:
        spec["knowledge"] = {
            "corpora": {
                "faq": {
                    "sources": [{"type": "file", "path": str(docs)}],
                    "chunking": "layout",
                    "retrieval": {**CORPUS["retrieval"], "not_found": not_found},
                    "sync": {"schedule": "0 */4 * * *"},
                }
            }
        }
        spec["agents"]["front"]["knowledge"] = ["faq"]
        spec["policies"] = {
            "verification": {
                "checks": {
                    "grounded": {"type": "citations", "min_citations": 1, "claims_must_cite": True}
                }
            },
            "escalation": {"rules": rules or []},
        }

    return edit


def _cite(request: ModelRequest) -> Message:
    """Answer from the last search result, citing it."""
    result = request.messages[-1].content[0]
    assert isinstance(result, ToolResultBlock)
    source = result.content["results"][0]["source"]
    return Message.assistant(f"We accept cash, debit and credit cards [{source}].")


async def _ask(client: Any, text: str) -> str:
    r = await client.post(
        "/channels/api",
        json={"contact": "ana@example.com", "text": text},
        headers={"authorization": f"Bearer {API_TOKEN}"},
    )
    assert r.status_code == 200, r.text
    return str(r.json()["replies"][0]["reply"])


def test_layout_chunking() -> None:
    chunks = chunk(FAQ)
    assert [(c.section, c.text[:20]) for c in chunks] == [
        ("Sonrisa FAQ", "Answers for our pati"),
        ("Sonrisa FAQ > Payment methods", "We accept cash, debi"),
        ("Sonrisa FAQ > Location", "We are at Av. Reform"),
        ("Sonrisa FAQ > Location > Opening hours", "Monday to Saturday, "),
    ]
    html = chunk(PRICES, "html")
    assert [c.section for c in html] == ["Prices > Cleaning", "Prices > Whitening"]
    assert "color" not in " ".join(c.text for c in html)  # styles are not content
    assert len(chunk(FAQ, mode="paragraph")) == 1  # headings ignored: one group of paragraphs

    long = " ".join(f"Sentence number {i} is here." for i in range(200))
    pieces = chunk(f"# T\n\n{long}")
    assert len(pieces) > 1 and all(len(c.text) <= MAX_CHARS for c in pieces)
    assert all(c.text.endswith(".") for c in pieces)  # cut at sentence ends


def test_citation_markers_never_look_like_pii() -> None:
    every = {"email", "phone", "curp", "rfc", "account"}
    for _ in range(500):
        text = f"We accept cards [kb:{chunk_id()}] and cash [kb:{chunk_id()}]."
        assert find(text, every) == []  # a tokenized marker would break the citation


async def test_store_sync_search_and_scope(db: Any, scope: Scope, tmp_path: Path) -> None:
    docs = _docs(tmp_path)
    corpora = {"faq": {"sources": [{"type": "file", "path": str(docs)}, {"type": "gdrive"}]}}
    corpora["faq"].update(CORPUS)
    kb = KnowledgeBase(db, scope, corpora)
    report = await kb.sync("faq")
    assert (report.added, report.updated, report.removed) == (2, 0, 0)
    assert report.skipped == [str(docs / "scan.pdf")] and report.unavailable == ["gdrive"]
    assert (await kb.sync("faq")).unchanged == 2

    [hit] = await kb.search("faq", "Which payment methods do you accept?")
    assert hit.section == "Sonrisa FAQ > Payment methods" and hit.score == 1.0
    assert terms("¿Dónde está la clínica?") == ["clinica"]  # accents folded, stop words out
    [price] = await kb.search("faq", "How much does whitening cost?")
    assert price.title == "Prices" and "3,500" in price.text
    assert await kb.search("faq", "orthodontic insurance coverage") == []  # not found
    weak = await kb.search("faq", "parking insurance coverage", min_score=0.0)
    assert weak and weak[0].score < 0.5  # found only below the threshold

    (docs / "faq.md").write_text(FAQ.replace("We do not accept checks.", "Checks are fine."))
    (docs / "more" / "prices.html").unlink()
    report = await kb.sync("faq")
    assert (report.updated, report.removed) == (1, 1)
    assert await kb.search("faq", "whitening price") == []  # deletion reached the index
    [doc] = await kb.documents("faq")
    assert doc["version"] == 2 and doc["origin"] == "file"

    await kb.put("faq", "cms://page/7", "Emergencies: call the clinic.", fmt="text")
    assert (await kb.search("faq", "emergencies"))[0].uri == "cms://page/7"
    assert (await kb.put("faq", "cms://page/7", "Emergencies: call the clinic.", fmt="text"))[
        1
    ] == "unchanged"
    await kb.sync("faq")  # pushed documents are not the files' to delete
    assert await kb.search("faq", "emergencies")

    other = KnowledgeBase(db, Scope(tenant_id="beta", instance_id="beta-desk"), corpora)
    assert await other.search("faq", "payment methods") == []  # nothing crosses tenants
    assert await kb.delete("faq", "cms://page/7") and not await kb.search("faq", "emergencies")


async def test_cited_answer_reaches_the_contact_with_sources(tmp_path: Path) -> None:
    script = [calls(("k1", "knowledge.search_faq", {"query": "payment methods"})), _cite]
    env = Env(tmp_path, script, edit=_kb_spec(_docs(tmp_path)))
    inst, _, client = await env.open()
    async with inst, client:
        codes = {i.code for i in inst.issues}
        assert "knowledge_format_unavailable" in codes  # the PDF is reported, not guessed at
        reply = await _ask(client, "¿Qué formas de pago aceptan?")
        assert reply == (
            "We accept cash, debit and credit cards [1].\n\n"
            "Sources:\n[1] Sonrisa FAQ — Payment methods"
        )
        result = env.provider.requests[1].messages[-1].content[0]
        assert isinstance(result, ToolResultBlock) and result.content["found"] is True
        assert len(result.content["results"]) == 1  # only chunks above min_score


async def test_uncited_answer_is_rewritten_once(tmp_path: Path) -> None:
    script = [
        calls(("k1", "knowledge.search_faq", {"query": "payment methods"})),
        Message.assistant("We accept cash, debit and credit cards, but no checks at all."),
        lambda req: _cite(
            ModelRequest(
                system="",
                messages=[m for m in req.messages if isinstance(m.content[0], ToolResultBlock)],
                tools=[],
            )
        ),
    ]
    env = Env(tmp_path, script, edit=_kb_spec(_docs(tmp_path)))
    inst, _, client = await env.open()
    async with inst, client:
        reply = await _ask(client, "¿Qué formas de pago aceptan?")
        assert reply.startswith("We accept cash, debit and credit cards [1].")  # only the rewrite
        note = env.provider.requests[2].messages[-1].text()
        assert note.startswith("[Automatic check: your last answer cites 0 source(s)")
        events = await inst.db.fetchall(
            "SELECT action FROM audit WHERE action = ?", ("citations_failed",)
        )
        assert len(events) == 1


async def test_invented_citations_become_not_found(tmp_path: Path) -> None:
    invented = Message.assistant("We accept bitcoin and gold bars [kb:zzzzzzzzzz].")
    script = [
        calls(("k1", "knowledge.search_faq", {"query": "payment methods"})),
        invented,
        invented,
    ]
    env = Env(tmp_path, script, edit=_kb_spec(_docs(tmp_path)))
    inst, _, client = await env.open()
    async with inst, client:
        reply = await _ask(client, "¿Aceptan bitcoin?")
        assert reply == "I could not find a sourced answer to that in our documents."
        assert "not retrieved: zzzzzzzzzz" in env.provider.requests[2].messages[-1].text()


async def test_not_found_says_so_and_escalates_by_rule(tmp_path: Path) -> None:
    script = [
        calls(("k1", "knowledge.search_faq", {"query": "orthodontic insurance coverage"})),
        Message.assistant("Our documents don't cover that; a person will follow up."),
    ]
    rules = [{"when": "knowledge.not_found and knowledge.corpus == 'faq'", "to": "human"}]
    env = Env(tmp_path, script, edit=_kb_spec(_docs(tmp_path), not_found="handoff", rules=rules))
    inst, _, client = await env.open()
    async with inst, client:
        reply = await _ask(client, "¿Cubren ortodoncia con mi seguro?")
        assert reply == "Our documents don't cover that; a person will follow up."  # no check
        result = env.provider.requests[1].messages[-1].content[0]
        assert isinstance(result, ToolResultBlock) and result.content["found"] is False
        assert "hand off" in result.content["message"]
        [item] = await inst.inbox.list(kind="escalation")
        assert item.title.startswith("faq has no answer: orthodontic insurance")


async def test_admin_documents_and_scheduled_sync(tmp_path: Path) -> None:
    docs = _docs(tmp_path)
    env = Env(tmp_path, [], edit=_kb_spec(docs))
    inst, headless, client = await env.open()
    async with inst, client:
        r = await client.get("/admin/knowledge/faq/documents", headers=ADMIN_H)
        assert sorted(d["title"] for d in r.json()) == ["Prices", "Sonrisa FAQ"]

        doc = {"uri": "cms://emergencies", "text": "<h1>Emergencies</h1><p>Call 555 0101.</p>",
               "format": "html"}  # fmt: skip
        r = await client.put("/admin/knowledge/faq/documents", json=doc, headers=ADMIN_H)
        assert r.json()["result"] == "added"
        r = await client.get("/admin/knowledge/faq/search?q=emergencies", headers=ADMIN_H)
        assert r.json()[0]["document"] == "Emergencies"
        r = await client.get("/admin/knowledge/faq/search?q=parking+fees+refund", headers=ADMIN_H)
        assert r.json() and r.json()[0]["below_min_score"] is True
        r = await client.delete(
            "/admin/knowledge/faq/documents?uri=cms://emergencies", headers=ADMIN_H
        )
        assert r.json() == {"removed": True}
        r = await client.put("/admin/knowledge/nope/documents", json=doc, headers=ADMIN_H)
        assert r.status_code == 404

        # the sync schedule picks up changed files
        await headless.start()
        (docs / "more" / "prices.html").write_text(PRICES.replace("800", "950"))
        env.clock.now += 4 * 3600 + 1
        await headless.worker().drain()
        [hit] = await inst.knowledge.search("faq", "cleaning cost")
        assert "950 MXN" in hit.text
        r = await client.post("/admin/knowledge/faq/sync", headers=ADMIN_H)
        assert r.json()["unchanged"] == 2
        assert (await client.get("/admin/audit/verify", headers=ADMIN_H)).json()["intact"]
