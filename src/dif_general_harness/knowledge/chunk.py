"""Turning documents into chunks (ARCHITECTURE §3.19).

``layout`` chunking follows the document's structure: headings (Markdown ``#`` lines, HTML
``<h1>``..``<h6>``) open a section, and a section's paragraphs are grouped up to
``MAX_CHARS``. A chunk never spans two sections, and it carries its heading path
("Billing > Refunds") so a search can match the heading and a citation can name it.
``paragraph`` chunking ignores headings and only groups paragraphs.

Formats read here: Markdown, plain text, HTML and CSV (as text). Anything else (PDF, DOCX,
images) needs the ``documents`` pack and is reported, not guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

MAX_CHARS = 1200
TEXT_SUFFIXES = {".md": "markdown", ".markdown": "markdown", ".txt": "text", ".csv": "text",
                 ".html": "html", ".htm": "html"}  # fmt: skip
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class Chunk:
    section: str
    text: str


def format_of(path: Path) -> str | None:
    return TEXT_SUFFIXES.get(path.suffix.lower())


class _HtmlToMarkdown(HTMLParser):
    """Just enough HTML: headings become ``#`` lines, blocks become paragraphs."""

    BLOCKS: ClassVar[set[str]] = {
        "p",
        "div",
        "li",
        "tr",
        "br",
        "section",
        "article",
        "table",
        "ul",
        "ol",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "nav", "footer", "head"):
            self.skip += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in self.BLOCKS:
            self.out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "nav", "footer", "head"):
            self.skip = max(0, self.skip - 1)
        elif re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCKS:
            self.out.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(" ".join(data.split()) if data.strip() else "")


def html_to_markdown(html: str) -> str:
    parser = _HtmlToMarkdown()
    parser.feed(html)
    parser.close()
    return "".join(parser.out)


def title_of(text: str, fmt: str, fallback: str) -> str:
    if fmt == "html":
        found = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        if found and found.group(1).strip():
            return " ".join(found.group(1).split())
        text = html_to_markdown(text)
    for line in text.splitlines():
        match = _HEADING.match(line.strip())
        if match and len(match.group(1)) == 1:
            return match.group(2)
    return fallback


def chunk(text: str, fmt: str = "markdown", mode: str = "layout") -> list[Chunk]:
    if fmt == "html":
        text = html_to_markdown(text)
    use_headings = mode == "layout" and fmt in ("markdown", "html")
    sections: list[tuple[str, list[str]]] = [("", [])]
    path: list[tuple[int, str]] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            sections[-1][1].append(" ".join(" ".join(paragraph).split()))
            paragraph.clear()

    for raw in text.splitlines():
        line = raw.strip()
        heading = _HEADING.match(line) if use_headings else None
        if heading:
            flush()
            level = len(heading.group(1))
            path = [(lvl, name) for lvl, name in path if lvl < level] + [(level, heading.group(2))]
            sections.append((" > ".join(name for _, name in path), []))
        elif not line:
            flush()
        else:
            paragraph.append(line)
    flush()

    chunks: list[Chunk] = []
    for section, paragraphs in sections:
        current: list[str] = []
        for para in paragraphs:
            for piece in _split_long(para):
                if current and sum(len(p) + 2 for p in current) + len(piece) > MAX_CHARS:
                    chunks.append(Chunk(section, "\n\n".join(current)))
                    current = []
                current.append(piece)
        if current:
            chunks.append(Chunk(section, "\n\n".join(current)))
        elif section and not paragraphs:
            continue  # a heading with nothing under it (its subsections carry the text)
    return chunks


def _split_long(paragraph: str) -> list[str]:
    """A paragraph longer than ``MAX_CHARS`` is cut at sentence ends."""
    if len(paragraph) <= MAX_CHARS:
        return [paragraph]
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
        while len(sentence) > MAX_CHARS:  # no sentence end in sight: cut hard
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:MAX_CHARS])
            sentence = sentence[MAX_CHARS:]
        if current and len(current) + 1 + len(sentence) > MAX_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces
