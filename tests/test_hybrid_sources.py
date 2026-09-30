"""G6: hybrid retrieval (keywords + embeddings) and knowledge sources beyond files."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from dif_general_harness.core.messages import ToolUseBlock
from dif_general_harness.core.scope import Scope
from dif_general_harness.knowledge import KnowledgeBase
from dif_general_harness.knowledge import sources as knowledge_sources
from dif_general_harness.providers import FakeProvider
from dif_general_harness.store.db import connect
from dif_general_harness.store.schema import migrate
from tests.support import Env
from tests.test_documents import make_pdf
from tests.test_file_triggers import FakeS3

CONCEPTS = {  # words that mean the same thing share an axis, as in a real embedding space
    **dict.fromkeys(["reembolso", "reembolsan", "devoluciones", "devolvemos", "dinero",
                     "refund"], 0),
    **dict.fromkeys(["horario", "abrimos", "abren", "hours", "open"], 1),
}  # fmt: skip


def meaning(text: str) -> list[float]:
    vector = [0.0] * 32
    for word in re.findall(r"\w+", text.lower()):
        axis = CONCEPTS.get(word)
        if axis is not None:
            vector[axis] += 3.0
        else:
            vector[2 + sum(map(ord, word)) % 30] += 0.3
    return vector


REFUNDS = "# Devoluciones\n\nDevolvemos su dinero en 30 días naturales si no está satisfecho."
HOURS = "# Horario\n\nAbrimos de lunes a viernes, de 9 a 18 h."


async def _kb(tmp_path: Path, mode: str, provider: FakeProvider) -> KnowledgeBase:
    db = await connect(f"sqlite:///{tmp_path / 'kb.db'}")
    await migrate(db)
    corpora = {"faq": {"retrieval": {"mode": mode, "min_score": 0.5, "min_similarity": 0.6}}}
    kb = KnowledgeBase(db, Scope(tenant_id="acme", instance_id="desk"), corpora)

    async def embed(texts: list[str]) -> tuple[list[list[float]], str]:
        result = await provider.embed(texts)
        return result.vectors, result.model

    kb.embedder = embed
    return kb


async def test_hybrid_finds_the_same_meaning_in_other_words(tmp_path: Path) -> None:
    provider = FakeProvider([], embed=meaning)
    kb = await _kb(tmp_path, "hybrid", provider)
    await kb.put("faq", "cms://refunds", REFUNDS)
    await kb.put("faq", "cms://hours", HOURS)
    assert sum(len(b) for b in provider.embedded) == 2  # each chunk once, at put

    [hit] = await kb.search("faq", "¿Me reembolsan?")  # no word in common with the answer
    assert hit.uri == "cms://refunds" and hit.score >= 0.6
    both = await kb.search("faq", "horario de devoluciones")
    assert {h.uri for h in both} == {"cms://refunds", "cms://hours"}
    assert await kb.search("faq", "estacionamiento gratuito") == []  # still "not found"

    (tmp_path / "k").mkdir()
    keyword = await _kb(tmp_path / "k", "keyword", provider)
    await keyword.put("faq", "cms://refunds", REFUNDS)
    assert await keyword.search("faq", "¿Me reembolsan?") == []  # keywords alone miss it
    await kb.db.close()
    await keyword.db.close()


async def test_a_down_embedding_model_falls_back_to_keywords(tmp_path: Path) -> None:
    provider = FakeProvider([], embed=meaning)
    kb = await _kb(tmp_path, "hybrid", provider)
    await kb.put("faq", "cms://hours", HOURS)

    async def down(texts: list[str]) -> tuple[list[list[float]], str]:
        raise ConnectionError("embedding endpoint unreachable")

    kb.embedder = down
    [hit] = await kb.search("faq", "horario lunes")
    assert hit.uri == "cms://hours"  # keyword scoring still answers

    calls: list[int] = []

    async def new_model(texts: list[str]) -> tuple[list[list[float]], str]:
        calls.append(len(texts))
        return [meaning(t) for t in texts], "embed-v2"

    kb.embedder = new_model
    await kb.db.execute("DELETE FROM knowledge_vectors")  # e.g. after a model change
    assert await kb.embed_missing("faq") == 1 and calls == [1]
    rows = await kb.db.fetchall("SELECT model FROM knowledge_vectors")
    assert [r["model"] for r in rows] == ["embed-v2"]
    await kb.db.close()


async def _plain_kb(tmp_path: Path, sources: list[Any]) -> KnowledgeBase:
    db = await connect(f"sqlite:///{tmp_path / 'kb.db'}")
    await migrate(db)
    corpora = {"docs": {"sources": sources, "retrieval": {"min_score": 0.3}}}
    return KnowledgeBase(db, Scope(tenant_id="acme", instance_id="desk"), corpora)


async def test_s3_prefixes_sync_incrementally(tmp_path: Path) -> None:
    s3 = FakeS3({
        "kb/refunds.md": REFUNDS.encode(),
        "kb/policy.pdf": make_pdf(["Garantia de 12 meses en implantes"]),
        "kb/logo.png": b"\x89PNG",
        "other/x.md": b"# Not ours",
    })  # fmt: skip
    kb = await _plain_kb(tmp_path, ["s3://acme-inbox/kb/"])
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(404)))
    kb.sources = lambda corpus, src: knowledge_sources.build(src, None, http, s3_client=s3)

    report = await kb.sync("docs")
    assert (report.added, report.skipped) == (2, ["s3://acme-inbox/kb/logo.png"])
    [hit] = await kb.search("docs", "garantia implantes")
    assert hit.uri == "s3://acme-inbox/kb/policy.pdf"

    s3.reads.clear()
    again = await kb.sync("docs")
    assert again.unchanged == 2 and s3.reads == ["kb/logo.png"]  # unchanged objects not re-read

    del s3.objects["kb/refunds.md"]
    gone = await kb.sync("docs")
    assert gone.removed == 1
    assert [d["uri"] for d in await kb.documents("docs")] == ["s3://acme-inbox/kb/policy.pdf"]

    def broken(**kw: Any) -> Any:
        raise ConnectionError("S3 is unreachable")

    s3.list_objects_v2 = broken  # type: ignore[method-assign]
    down = await kb.sync("docs")
    assert down.removed == 0 and "unreachable" in down.unavailable[0]  # a source that is down
    assert len(await kb.documents("docs")) == 1  # removes nothing
    await kb.db.close()


def _service_account() -> dict[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()  # fmt: skip
    return {"client_email": "kb@acme.iam.gserviceaccount.com", "private_key": pem,
            "token_uri": "https://oauth2.googleapis.com/token"}  # fmt: skip


async def test_google_drive_folders_sync(tmp_path: Path) -> None:
    gapps = "application/vnd.google-apps."
    listing = {
        "root": [
            {"id": "sub", "name": "Procedimientos", "mimeType": gapps + "folder"},
            {"id": "doc1", "name": "Politicas", "mimeType": gapps + "document",
             "modifiedTime": "2026-09-01T10:00:00Z"},
            {"id": "pdf1", "name": "garantia.pdf", "mimeType": "application/pdf",
             "md5Checksum": "a1"},
        ],
        "sub": [{"id": "md1", "name": "limpieza.md", "mimeType": "text/markdown",
                 "md5Checksum": "b2"}],
    }  # fmt: skip
    seen: list[str] = []

    def drive(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "oauth2.googleapis.com":
            assert b"jwt-bearer" in request.content
            return httpx2.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer tok"
        path = request.url.path
        seen.append(path)
        if path == "/drive/v3/files":
            folder = request.url.params["q"].split("'")[1]
            return httpx2.Response(200, json={"files": listing[folder]})
        if path == "/drive/v3/files/doc1/export":
            assert request.url.params["mimeType"] == "text/plain"
            return httpx2.Response(200, text="Reembolsos: devolvemos el pago en 30 dias.")
        if path == "/drive/v3/files/pdf1":
            return httpx2.Response(200, content=make_pdf(["Garantia de 12 meses en implantes"]))
        if path == "/drive/v3/files/md1":
            return httpx2.Response(200, text="# Limpieza\n\nLa limpieza dental dura 45 minutos.")
        return httpx2.Response(404)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(drive))
    kb = await _plain_kb(tmp_path, [{"type": "gdrive", "folder_id": "root"}])
    key = json.dumps(_service_account())
    kb.sources = lambda corpus, src: knowledge_sources.build(src, key, http)
    report = await kb.sync("docs")
    assert report.added == 3 and not report.unavailable
    titles = {d["uri"]: d["title"] for d in await kb.documents("docs")}
    assert titles == {"gdrive://doc1": "Politicas", "gdrive://pdf1": "garantia.pdf",
                      "gdrive://md1": "limpieza.md"}  # fmt: skip
    [hit] = await kb.search("docs", "cuanto dura la limpieza")
    assert hit.uri == "gdrive://md1"
    seen.clear()
    assert (await kb.sync("docs")).unchanged == 3
    assert all(p == "/drive/v3/files" for p in seen)  # only listings: nothing re-downloaded
    await kb.db.close()


async def test_web_pages_sync(tmp_path: Path) -> None:
    html = "<html><h1>Preguntas</h1><p>Aceptamos tarjetas y efectivo.</p></html>"
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(
        lambda r: httpx2.Response(200, text=html, headers={"content-type": "text/html"})
    ))  # fmt: skip
    kb = await _plain_kb(tmp_path, ["https://acme.example/faq"])
    kb.sources = lambda corpus, src: knowledge_sources.build(src, None, http)
    assert (await kb.sync("docs")).added == 1
    [hit] = await kb.search("docs", "tarjetas o efectivo")
    assert hit.uri == "https://acme.example/faq" and "# Preguntas" not in hit.text
    assert (await kb.sync("docs")).unchanged == 1
    await kb.db.close()


def _hybrid(spec: dict[str, Any], folder: Path) -> None:
    spec["models"]["roles"]["embedding"] = {"provider": "openai-compatible",
                                            "model": "text-embedding-3-small"}  # fmt: skip
    spec["models"]["providers"]["openai-compatible"] = {"base_url": "http://llm.internal/v1"}
    spec["knowledge"] = {"corpora": {"faq": {
        "sources": [{"type": "file", "path": str(folder)}],
        "retrieval": {"mode": "hybrid", "min_score": 0.5, "min_similarity": 0.6},
    }}}  # fmt: skip
    spec["agents"]["front"]["knowledge"] = ["faq"]


async def test_instances_embed_their_corpora_and_charge_it(tmp_path: Path) -> None:
    folder = tmp_path / "faq"
    folder.mkdir()
    (folder / "devoluciones.md").write_text(REFUNDS)
    env = Env(tmp_path, [], edit=lambda spec: _hybrid(spec, folder))
    env.provider = FakeProvider([], embed=meaning)
    inst, headless, client = await env.open()
    async with inst, client:
        assert "keyword_only" not in {i.code for i in inst.issues}
        tools = headless.agent("front").tools
        found = await tools.execute(
            ToolUseBlock(id="k", name="knowledge.search_faq", input={"query": "¿me reembolsan?"})
        )
        assert found.content["found"] is True
        rows = await inst.db.fetchall("SELECT role, model FROM usage WHERE role = 'embedding'")
        assert rows and rows[0]["model"] == "fake-embedding"
