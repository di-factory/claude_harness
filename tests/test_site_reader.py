"""A whole web site as knowledge: the crawler (its own links, pages only, a page limit), and
the setup reading it once and having a model write the knowledge file. Offline: the site
is a ``MockTransport``, the writer a ``FakeProvider``."""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

from dif_general_harness.constructor import build
from dif_general_harness.constructor.setup import Setup
from dif_general_harness.core.messages import Message
from dif_general_harness.knowledge import sources as knowledge_sources
from dif_general_harness.knowledge.sources import SiteKnowledge
from dif_general_harness.providers import FakeProvider
from dif_general_harness.spec import PackCatalog
from tests.test_setup_wizard import ABOUT, KEY

PAGES = {
    "/": "<html><title>Reparo | Home</title><body><h1>Reparo</h1><p>We fix phones.</p>"
    '<a href="/about/">About</a> <a href="/about/#team">Team</a> <a href="/static/a.css">x</a>'
    ' <a href="https://elsewhere.example/">out</a> <a href="/prices/?ref=nav">Prices</a>'
    ' <a href="/gone/">old</a> <a href="mailto:hola@reparo.mx">mail</a></body></html>',
    "/about/": "<html><title>About Reparo</title><p>Founded in 2015 in Guadalajara.</p>"
    '<a href="/">Home</a></html>',
    "/prices/": "<html><title>Prices</title><p>Screen repair: 900 MXN.</p></html>",
}


def _site() -> tuple[httpx2.AsyncClient, list[str]]:
    asked: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.append(str(request.url))
        assert request.url.host == "reparo.example"  # never leaves the site
        body = PAGES.get(request.url.path)
        if body is None:
            return httpx2.Response(404, text="no")
        return httpx2.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), asked


async def test_a_site_is_read_by_its_own_links() -> None:
    http, asked = _site()
    site = SiteKnowledge("https://reparo.example/", http)
    entries = await site.entries()
    assert [e.uri for e in entries] == [
        "https://reparo.example/", "https://reparo.example/about/",
        "https://reparo.example/prices/",
    ]  # fmt: skip
    assert [e.title for e in entries] == ["Reparo | Home", "About Reparo", "Prices"]
    assert not any("css" in u or "elsewhere" in u for u in asked)  # pages of this site only
    assert "https://reparo.example/gone/" in asked  # a broken link is skipped, not fatal
    text = await site.read(entries[2])
    assert text is not None and "Screen repair: 900 MXN." in text.text
    assert len(await SiteKnowledge("https://reparo.example/", http, max_pages=1).entries()) == 1
    await http.aclose()


def test_a_site_source_is_written_with_a_star_and_needs_no_credentials() -> None:
    assert knowledge_sources.normalize("https://reparo.example/*") == {
        "type": "site", "url": "https://reparo.example/"}  # fmt: skip
    assert knowledge_sources.normalize("https://reparo.example/faq")["type"] == "url"
    assert knowledge_sources.is_public_web({"type": "site", "url": "https://x.example/"})
    assert not knowledge_sources.is_public_web({"type": "gdrive", "folder_id": "f"})


def _answers(tmp_path: Path, *sources: str) -> dict[str, object]:
    return {
        "tenant.id": "reparo", "tenant.name": "Reparo", "values.assistant_name": "Reparo",
        "values.corpus_sources": list(sources or ["https://reparo.example"]),
        "values.support_email": "a@reparo.mx", "values.main_model": "m",
        "values.fast_model": "f", **ABOUT,
    }  # fmt: skip


FILE = "reparo-conversational-rag.knowledge/reparo.example.md"
WRITTEN = """# Reparo

## What does Reparo do?
Reparo fixes phones; founded in 2015 in Guadalajara.
Source: https://reparo.example/, https://reparo.example/about/

## How much does a screen repair cost?
900 MXN.
Source: https://reparo.example/prices/
"""


def test_the_setup_reads_the_site_and_a_model_writes_the_knowledge_file(
    examples: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = PackCatalog(roots=[examples])
    out = tmp_path / "clients"
    first = build(catalog, ["conversational-rag"], out, answers=_answers(tmp_path))
    assert first.ok, first.problems
    writer = FakeProvider([Message.assistant(WRITTEN)])
    setup = Setup([examples], out, ask=lambda _: "", writer=lambda _: writer)  # Enter: yes
    setup.key = KEY
    setup.site_http, _ = _site()
    result = setup.read_sites(first)
    knowledge = out / "reparo-conversational-rag.knowledge" / "reparo.example.md"
    assert knowledge.read_text() == WRITTEN
    prompt = writer.requests[0].messages[0].text()
    assert "=== Page: https://reparo.example/prices/" in prompt and "900 MXN" in prompt
    assert writer.requests[0].system.startswith("You turn the pages of a web site")
    spec = json.loads(result.spec_path.read_text())
    assert spec["values"]["corpus_sources"] == [
        "reparo-conversational-rag.knowledge/reparo.example.md"
    ]
    assert result.ok and result.resolved is not None
    assert "3 page(s) read" in capsys.readouterr().out

    again = build(catalog, ["conversational-rag"], out, answers=_answers(tmp_path))
    setup.ask = lambda _: ""  # a rebuild: Enter keeps the file already written
    kept = setup.read_sites(again)
    assert len(writer.requests) == 1  # not read or written again
    assert json.loads(kept.spec_path.read_text())["values"]["corpus_sources"] == [FILE]


def test_without_the_writer_model_the_pages_are_kept_as_they_are(
    examples: Path, tmp_path: Path
) -> None:
    catalog = PackCatalog(roots=[examples])
    first = build(catalog, ["conversational-rag"], tmp_path, answers=_answers(tmp_path))
    setup = Setup([examples], tmp_path, ask=lambda _: "")
    setup.site_http, _ = _site()
    setup.read_sites(first)
    text = (tmp_path / "reparo-conversational-rag.knowledge" / "reparo.example.md").read_text()
    assert text.startswith("# Reparo") and "## About Reparo" in text
    assert "Source: https://reparo.example/prices/" in text


def test_a_page_address_reads_that_page_only(examples: Path, tmp_path: Path) -> None:
    from dif_general_harness.constructor.site_reader import file_for, whole_site

    assert whole_site("https://reparo.example") and whole_site("https://reparo.example/")
    assert whole_site("https://reparo.example/blog/*")
    assert not whole_site("https://reparo.example/prices/")
    assert file_for("https://www.reparo.example/") == "reparo.example.md"
    assert file_for("https://reparo.example/prices/?x=1") == "reparo.example-prices.md"

    catalog = PackCatalog(roots=[examples])
    first = build(catalog, ["conversational-rag"], tmp_path,
                  answers=_answers(tmp_path, "https://reparo.example/prices/", "docs"))  # fmt: skip
    writer = FakeProvider([Message.assistant("# Reparo\n\n## Prices?\n900 MXN.\n")])
    setup = Setup([examples], tmp_path, ask=lambda _: "", writer=lambda _: writer)
    setup.key = KEY
    setup.site_http, asked = _site()
    result = setup.read_sites(first)
    assert asked == ["https://reparo.example/prices/"]  # that page, no other
    sources = json.loads(result.spec_path.read_text())["values"]["corpus_sources"]
    assert sources == ["reparo-conversational-rag.knowledge/reparo.example-prices.md", "docs"]
