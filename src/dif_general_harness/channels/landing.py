"""The business's landing page (``GET /``), served next to the web chat.

It is built from what the client already answered, so it never says anything the agent
would not: the business name, the first FAQ section as the introduction, the other FAQ
sections as cards, the opening hours (``values.business_hours``) and a WhatsApp button when a
gateway channel has a real number. The chat opens in a panel (a full page on phones).
One self-contained HTML file, no outside requests.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..spec.schema import SolutionSpec

_PHONE = re.compile(r"^\+\d{8,15}$")
TEXTS = {
    "es": {"chat": "Chatea con nosotros", "whatsapp": "WhatsApp", "hours": "Horario",
           "close": "Cerrar", "note": "Te responde nuestro asistente; una persona del equipo"
           " revisa cuando hace falta."},
    "en": {"chat": "Chat with us", "whatsapp": "WhatsApp", "hours": "Opening hours",
           "close": "Close", "note": "Our assistant answers; a person from the team reviews"
           " when needed."},
}  # fmt: skip


def faq_sections(spec: SolutionSpec, data: dict[str, Any]) -> list[tuple[str, str]]:
    """(heading, body) from the instance's Markdown FAQ files, in order."""
    out: list[tuple[str, str]] = []
    corpora = ((data.get("knowledge") or {}).get("corpora")) or {}
    for corpus in corpora.values():
        for src in (corpus or {}).get("sources") or []:
            if not (isinstance(src, dict) and src.get("type") == "file"):
                continue
            path = Path(str(src.get("path") or ""))
            if path.suffix.lower() != ".md" or not path.is_file():
                continue
            heading: str | None = None
            lines: list[str] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("## "):
                    if heading and "\n".join(lines).strip():
                        out.append((heading, "\n".join(lines).strip()))
                    heading, lines = line[3:].strip(), []
                elif heading is not None:
                    lines.append(line)
            if heading and "\n".join(lines).strip():
                out.append((heading, "\n".join(lines).strip()))
    return out


def _paragraphs(text: str) -> str:
    """Plain text with blank-line paragraphs and '- ' lists; everything escaped."""
    parts = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if lines and all(ln.startswith(("- ", "* ")) for ln in lines):
            items = "".join(f"<li>{html.escape(ln[2:])}</li>" for ln in lines)
            parts.append(f"<ul>{items}</ul>")
        elif lines:
            parts.append("<p>" + "<br>".join(html.escape(ln) for ln in lines) + "</p>")
    return "".join(parts)


def _hours(value: Any) -> str:
    if isinstance(value, dict):
        rows = "".join(
            f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
            for k, v in value.items()
        )
        return f"<table>{rows}</table>"
    return _paragraphs(str(value).replace(";", "\n")) if value else ""


def _whatsapp(spec: SolutionSpec) -> str | None:
    for channel in spec.channels.values():
        number = (channel.address or "").replace(" ", "")
        if channel.type == "gateway" and _PHONE.match(number):
            return number.lstrip("+")
    return None


def render_landing(spec: SolutionSpec, data: dict[str, Any], chat: str) -> str:
    locale = (spec.solution.locale or "en")[:2]
    t = TEXTS.get(locale, TEXTS["en"])
    values = data.get("values") or {}
    name = str(values.get("business_name") or (spec.tenant.name if spec.tenant else "") or
               spec.solution.name or "")  # fmt: skip
    sections = faq_sections(spec, data)
    intro, cards = (sections[0], sections[1:]) if sections else (None, [])
    card_html = "".join(
        f'<section class="card"><h2>{html.escape(h)}</h2>{_paragraphs(body)}</section>'
        for h, body in cards
    )
    hours = _hours(values.get("business_hours"))
    if hours:
        card_html += f'<section class="card"><h2>{html.escape(t["hours"])}</h2>{hours}</section>'

    wa = _whatsapp(spec)
    wa_html = (
        f'<a class="btn ghost" href="https://wa.me/{quote(wa)}" rel="noopener">{t["whatsapp"]}</a>'
        if wa else ""
    )  # fmt: skip
    chat_url = f"/chat/{quote(chat)}"
    return (
        _PAGE.replace("__LANG__", html.escape(locale))
        .replace("__NAME__", html.escape(name))
        .replace("__INTRO__", _paragraphs(intro[1]) if intro else "")
        .replace("__CARDS__", card_html)
        .replace("__WHATSAPP__", wa_html)
        .replace("__CHAT_URL__", chat_url)
        .replace("__CHAT__", html.escape(t["chat"]))
        .replace("__CLOSE__", html.escape(t["close"]))
        .replace("__NOTE__", html.escape(t["note"]))
    )


_PAGE = """<!doctype html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__NAME__</title>
<style>
:root{--bg:#f6f6f3;--panel:#fff;--ink:#1d1d1b;--muted:#5f5f5a;--line:#e3e3de;
--accent:#1f5f4a;--accent-ink:#fff;--soft:#e8f0ec}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1d1d1b;--ink:#ecece8;
--muted:#a3a39d;--line:#2e2e2b;--accent:#5fb394;--accent-ink:#0f1d17;--soft:#1f2b26}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);
font:17px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:0 20px}
header.hero{background:var(--soft);border-bottom:1px solid var(--line)}
.hero .wrap{padding-top:72px;padding-bottom:64px}
.hero h1{font-size:clamp(32px,6vw,52px);line-height:1.1;margin:0 0 18px;letter-spacing:-.02em}
.hero .intro{max-width:680px;color:var(--muted);font-size:19px}
.hero .intro p{margin:0 0 10px}
.actions{display:flex;flex-wrap:wrap;gap:12px;margin-top:28px}
.btn{display:inline-block;border-radius:999px;padding:13px 24px;font-weight:600;
text-decoration:none;border:2px solid var(--accent);cursor:pointer;font:inherit;
font-weight:600}
.btn.primary{background:var(--accent);color:var(--accent-ink)}
.btn.ghost{background:transparent;color:var(--accent)}
main .wrap{padding-top:48px;padding-bottom:96px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:22px 24px}
.card h2{font-size:18px;margin:0 0 10px}
.card p{margin:0 0 8px;color:var(--muted)}.card ul{margin:0;padding-left:20px;color:var(--muted)}
.card table{border-collapse:collapse;color:var(--muted)}
.card th{text-align:left;padding:2px 16px 2px 0;font-weight:600;color:var(--ink)}
footer{border-top:1px solid var(--line);color:var(--muted);font-size:14px}
footer .wrap{padding-top:20px;padding-bottom:28px}
#bubble{position:fixed;right:20px;bottom:20px;z-index:10;box-shadow:0 6px 24px rgba(0,0,0,.18)}
#panel{position:fixed;right:20px;bottom:88px;width:390px;height:min(620px,calc(100vh - 120px));
border:1px solid var(--line);border-radius:16px;overflow:hidden;background:var(--panel);
box-shadow:0 12px 40px rgba(0,0,0,.22);z-index:10;display:none}
#panel.open{display:block}#panel iframe{width:100%;height:100%;border:0}
</style>
</head>
<body>
<header class="hero"><div class="wrap">
<h1>__NAME__</h1>
<div class="intro">__INTRO__</div>
<div class="actions">
<a class="btn primary" href="__CHAT_URL__" data-chat>__CHAT__</a>
__WHATSAPP__
</div>
</div></header>
<main><div class="wrap"><div class="grid">__CARDS__</div></div></main>
<footer><div class="wrap">__NOTE__</div></footer>
<a id="bubble" class="btn primary" href="__CHAT_URL__" data-chat>__CHAT__</a>
<div id="panel" role="dialog" aria-label="__CHAT__"></div>
<script>
const panel = document.getElementById("panel");
const bubble = document.getElementById("bubble");
function toggle(e) {
  if (window.innerWidth < 700) return;  // phones: the chat opens as its own page
  e.preventDefault();
  if (!panel.firstChild) {
    const frame = document.createElement("iframe");
    frame.src = bubble.getAttribute("href"); frame.title = bubble.textContent;
    panel.appendChild(frame);
  }
  const open = panel.classList.toggle("open");
  bubble.textContent = open ? "__CLOSE__" : "__CHAT__";
}
document.querySelectorAll("[data-chat]").forEach((a) => a.addEventListener("click", toggle));
</script>
</body>
</html>
"""
