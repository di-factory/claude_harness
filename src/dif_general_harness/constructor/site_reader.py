"""A client's web site as one knowledge file: read every page, then a model writes it up.

A site source (``https://site/*``) can be read live by the running instance, page by page.
For a small business site it is better to read it once here, at setup time, and have a model
turn its pages into one Markdown file of questions a visitor would ask, each answered only
from what the pages say, with the page it came from. The file is part of the signed
solution, so the agent answers from exactly what Di-Factory approved; running the setup
again (reuse, rebuild) reads the site again on request.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx2

from ..knowledge.sources import SITE_PAGES, SiteKnowledge

PAGE_CHARS = 12_000  # per page, after conversion to Markdown
TOTAL_CHARS = 240_000  # all pages together (about 60k tokens)

WRITER_SYSTEM = """You turn the pages of a web site into one Markdown knowledge file that a
question-answering assistant will search and cite.

Write:
- a first line `# <the business or organization's name>`;
- then sections, each `## <a question a visitor would ask>` (what it is, who runs it,
  services, prices and timelines, how it works, industries or clients, locations, contact,
  hours, policies; whatever the pages cover), followed by a short, self-contained answer;
- after each answer, a line `Source: <the page URL it came from>` (several separated by
  commas when it combines pages).

Rules: only facts the pages state (names, prices, times, phone numbers, emails and
addresses copied exactly); never invent or guess; no marketing filler; merge what several
pages repeat; skip navigation, cookie and legal boilerplate. Write in the site's own
language. Answer with the Markdown only."""


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    text: str  # Markdown


async def read_site(
    url: str, *, max_pages: int = SITE_PAGES, http: httpx2.AsyncClient | None = None
) -> list[Page]:
    """Every page of the site under ``url`` (its own links, the same host and path)."""
    from ..tools.packs.general import guarded_client

    client = http or guarded_client()
    try:
        site = SiteKnowledge(url, client, max_pages)
        pages = []
        for entry in await site.entries():
            text = await site.read(entry)
            if text is not None and text.text.strip():
                pages.append(Page(entry.uri, entry.title, text.text.strip()))
        return pages
    finally:
        if http is None:
            await client.aclose()


def pages_prompt(pages: list[Page]) -> str:
    """The pages for the writer, each cut to a fair share of the total."""
    out, used = [], 0
    for page in pages:
        body = page.text[:PAGE_CHARS]
        if used + len(body) > TOTAL_CHARS:
            body = body[: max(0, TOTAL_CHARS - used)]
        if not body:
            break
        used += len(body)
        out.append(f"=== Page: {page.url}\nTitle: {page.title}\n\n{body}")
    return "\n\n".join(out)


def raw_markdown(pages: list[Page], name: str) -> str:
    """The pages as they are (no model): one section per page."""
    parts = [f"# {name}", ""]
    for page in pages:
        parts += [f"## {page.title}", page.text, f"Source: {page.url}", ""]
    return "\n".join(parts)


def site_entries(values: object) -> list[str]:
    """The ``https://.../*`` site sources among an instance's values."""
    found: list[str] = []
    items = values.values() if isinstance(values, dict) else []
    for value in items:
        for item in value if isinstance(value, list) else [value]:
            if (
                isinstance(item, str)
                and item.startswith(("https://", "http://"))
                and (item.endswith("/*"))
            ):
                found.append(item)
    return found


def file_for(url: str) -> str:
    """The knowledge file name for a site, e.g. ``di-factory.biz.md``."""
    host = url.split("://", 1)[1].split("/", 1)[0]
    return f"{host.removeprefix('www.')}.md"
