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
DEFAULT_PRIMARY, DEFAULT_ACCENT = "#1f5f4a", "#d9902f"
TEXTS = {
    "es": {"chat": "Chatea con nosotros", "whatsapp": "WhatsApp", "hours": "Horario",
           "close": "Cerrar", "map": "Ver en el mapa", "facts": "De un vistazo",
           "where": "Dónde", "pay": "Pagos", "band": "¿Tienes una pregunta?",
           "band_sub": "Nuestro asistente responde al momento, a cualquier hora.",
           "note": "Te responde nuestro asistente; una persona del equipo revisa cuando hace"
           " falta."},
    "en": {"chat": "Chat with us", "whatsapp": "WhatsApp", "hours": "Opening hours",
           "close": "Close", "map": "View on the map", "facts": "At a glance",
           "where": "Where", "pay": "Payment", "band": "Have a question?",
           "band_sub": "Our assistant answers right away, at any hour.",
           "note": "Our assistant answers; a person from the team reviews when needed."},
}  # fmt: skip

# Icons (24px, stroke = currentColor), chosen by words in a section's heading.
_ICON_PATHS = {
    "services": '<path d="M12 3l1.9 4.6L18.5 9l-4.6 1.9L12 15.5l-1.9-4.6L5.5 9l4.6-1.4z"/>'
    '<path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
    "price": '<path d="M20.6 13.4l-7.2 7.2a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/>'
    '<circle cx="7.5" cy="7.5" r="1.5"/>',
    "where": '<path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z"/>'
    '<circle cx="12" cy="9.5" r="2.5"/>',
    "pay": '<rect x="2.5" y="5" width="19" height="14" rx="2"/><path d="M2.5 10h19M6.5 15h4"/>',
    "cancel": '<rect x="3.5" y="4.5" width="17" height="16" rx="2"/>'
    '<path d="M3.5 9.5h17M8 2.5v4M16 2.5v4M10 13l4 4M14 13l-4 4"/>',
    "emergency": '<path d="M12 3l9.5 17h-19z"/><path d="M12 10v4M12 17.5v.01"/>',
    "hours": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "about": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.01"/>',
}
_ICON_WORDS = [
    ("emergency", ("emergenc", "urgenc")),
    ("cancel", ("cancel", "reprogram", "reschedul", "late")),
    ("pay", ("pay", "pago", "tarjeta", "card")),
    ("price", ("cost", "price", "precio", "cuesta", "tarifa")),
    ("where", ("locat", "where", "dónde", "donde", "ubica", "direcci", "address")),
    ("hours", ("hour", "horario", "abren", "open")),
    ("services", ("servic", "offer", "ofrec", "tratamiento", "product")),
]


def _topic(heading: str) -> str:
    low = heading.lower()
    return next((topic for topic, words in _ICON_WORDS if any(w in low for w in words)), "about")


def _icon(topic: str) -> str:
    return (
        '<span class="icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="22" height="22"'
        ' fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"'
        f' stroke-linejoin="round">{_ICON_PATHS[topic]}</svg></span>'
    )


def _hex(code: str | None, default: str) -> str:
    return code if code and re.fullmatch(r"#[0-9a-fA-F]{6}", code) else default


def _rgb(code: str) -> tuple[int, int, int]:
    return int(code[1:3], 16), int(code[3:5], 16), int(code[5:7], 16)


def _on(code: str) -> str:
    """Readable text on a color: white or near-black, by relative luminance."""

    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in _rgb(code))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#ffffff" if luminance < 0.42 else "#14140f"


def _theme(spec: SolutionSpec) -> str:
    colors = spec.branding.colors if spec.branding and spec.branding.colors else None
    primary = _hex(colors.primary if colors else None, DEFAULT_PRIMARY)
    accent = _hex(colors.accent if colors else None, DEFAULT_ACCENT)
    r, g, b = _rgb(primary)
    ar, ag, ab = _rgb(accent)
    return (
        f"--brand:{primary};--on-brand:{_on(primary)};--brand-rgb:{r},{g},{b};"
        f"--accent:{accent};--on-accent:{_on(accent)};--accent-rgb:{ar},{ag},{ab}"
    )


def _logo(spec: SolutionSpec, name: str) -> str:
    logo = spec.branding.logo if spec.branding else None
    if logo and logo.startswith("data:image/"):
        return f'<img class="logo" src="{html.escape(logo)}" alt="{html.escape(name)}">'
    initial = (name.strip()[:1] or "•").upper()
    return f'<span class="monogram" aria-hidden="true">{html.escape(initial)}</span>'


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


def _fact(label: str, body: str) -> str:
    first = next((ln.strip("-* ").strip() for ln in body.splitlines() if ln.strip()), "")
    if not first:
        return ""
    return f"<div><dt>{html.escape(label)}</dt><dd>{html.escape(first[:90])}</dd></div>"


def render_landing(spec: SolutionSpec, data: dict[str, Any], chat: str) -> str:
    locale = (spec.solution.locale or "en")[:2]
    t = TEXTS.get(locale, TEXTS["en"])
    values = data.get("values") or {}
    name = str(values.get("business_name") or (spec.tenant.name if spec.tenant else "") or
               spec.solution.name or "")  # fmt: skip
    sections = faq_sections(spec, data)
    intro, cards = (sections[0], sections[1:]) if sections else (None, [])
    card_html, facts = [], {}
    for heading, body in cards:
        topic = _topic(heading)
        extra = ""
        if topic == "where":
            place = " ".join(body.split())[:200]
            maps = "https://www.google.com/maps/search/?api=1&amp;query=" + quote(place)
            extra = f'<a class="more" href="{maps}" rel="noopener" target="_blank">{t["map"]} →</a>'
        if topic in ("where", "pay") and topic not in facts:
            facts[topic] = _fact(t["where"] if topic == "where" else t["pay"], body)
        card_html.append(
            f'<section class="card">{_icon(topic)}<h2>{html.escape(heading)}</h2>'
            f"{_paragraphs(body)}{extra}</section>"
        )
    hours = _hours(values.get("business_hours"))
    if hours:
        card_html.append(
            f'<section class="card">{_icon("hours")}<h2>{html.escape(t["hours"])}</h2>'
            f"{hours}</section>"
        )
        raw = values.get("business_hours")
        first = next(iter(raw.items())) if isinstance(raw, dict) and raw else None
        summary = f"{first[0]} {first[1]}" if first else str(raw or "")
        facts["hours"] = (f"<div><dt>{html.escape(t['hours'])}</dt>"
                          f"<dd>{html.escape(summary[:90])}</dd></div>")  # fmt: skip
    eyebrow = ""
    if hours:
        raw_hours = values.get("business_hours")
        text = ("; ".join(f"{k} {v}" for k, v in raw_hours.items())
                if isinstance(raw_hours, dict) else str(raw_hours))  # fmt: skip
        label = f"{html.escape(t['hours'])}: {html.escape(text[:60])}"
        eyebrow = f'<span class="eyebrow">{label}</span>'
    fact_html = "".join(facts.get(k, "") for k in ("where", "hours", "pay"))
    facts_box = (f'<aside class="facts"><h3>{t["facts"]}</h3><dl>{fact_html}</dl></aside>'
                 if fact_html else "")  # fmt: skip

    wa = _whatsapp(spec)
    wa_html = (
        f'<a class="btn ghost" href="https://wa.me/{quote(wa)}" rel="noopener">{t["whatsapp"]}</a>'
        if wa else ""
    )  # fmt: skip
    chat_url = f"/chat/{quote(chat)}"
    return (
        _PAGE.replace("__LANG__", html.escape(locale))
        .replace("__THEME__", _theme(spec))
        .replace("__LOGO__", _logo(spec, name))
        .replace("__NAME__", html.escape(name))
        .replace("__INTRO__", _paragraphs(intro[1]) if intro else "")
        .replace("__FACTS__", facts_box)
        .replace("__EYEBROW__", eyebrow)
        .replace("__CARDS__", "".join(card_html))
        .replace("__WHATSAPP__", wa_html)
        .replace("__CHAT_URL__", chat_url)
        .replace("__BAND__", html.escape(t["band"]))
        .replace("__BAND_SUB__", html.escape(t["band_sub"]))
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
:root{__THEME__;--bg:#fbfaf7;--panel:#ffffff;--ink:#17171a;--muted:#5d5d63;--line:#e9e7e1;
--shadow:0 1px 2px rgba(20,20,30,.05),0 8px 28px rgba(20,20,30,.07)}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--panel:#1b1b1f;--ink:#efeff2;
--muted:#a6a6ae;--line:#2b2b31;--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 28px rgba(0,0,0,.35)}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);
font:17px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
-webkit-font-smoothing:antialiased}
a{color:inherit}
.wrap{max-width:1120px;margin:0 auto;padding:0 20px}
nav{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 82%,transparent);
backdrop-filter:saturate(1.4) blur(10px);border-bottom:1px solid var(--line)}
nav .wrap{display:flex;align-items:center;justify-content:space-between;gap:12px;height:64px}
.brand{display:flex;align-items:center;gap:12px;font-weight:700;letter-spacing:-.01em;
text-decoration:none;min-width:0}
.brand span.name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.logo{height:38px;width:auto;max-width:140px;object-fit:contain;border-radius:8px}
.monogram{display:grid;place-items:center;width:38px;height:38px;border-radius:12px;
background:var(--brand);color:var(--on-brand);font-weight:800;flex:none}
.hero{position:relative;background:
radial-gradient(640px 420px at 92% -8%,rgba(var(--brand-rgb),.22),transparent 70%),
radial-gradient(560px 380px at -6% 105%,rgba(var(--accent-rgb),.16),transparent 70%)}
.hero .wrap{position:relative;z-index:1;display:grid;grid-template-columns:1.35fr .9fr;
gap:48px;align-items:center;padding-top:84px;padding-bottom:88px}
.eyebrow{display:inline-flex;align-items:center;gap:8px;font-size:14px;font-weight:600;
color:var(--brand);background:rgba(var(--brand-rgb),.1);padding:6px 12px;border-radius:999px}
@media (prefers-color-scheme:dark){.eyebrow{color:var(--ink)}}
.eyebrow::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--accent)}
.hero h1{font-size:clamp(36px,6.2vw,64px);line-height:1.05;margin:18px 0 18px;
letter-spacing:-.03em}
.hero .intro{max-width:620px;color:var(--muted);font-size:19px}
.hero .intro p{margin:0 0 10px}
.actions{display:flex;flex-wrap:wrap;gap:12px;margin-top:30px}
.btn{display:inline-flex;align-items:center;gap:8px;border-radius:999px;padding:13px 24px;
text-decoration:none;border:2px solid var(--brand);cursor:pointer;font:inherit;
font-weight:650;transition:transform .15s ease,box-shadow .15s ease}
.btn:hover{transform:translateY(-1px);box-shadow:var(--shadow)}
.btn.primary{background:var(--brand);color:var(--on-brand)}
.btn.ghost{background:transparent;color:var(--ink)}
.btn.small{padding:8px 16px;font-size:15px}
.facts{background:var(--panel);border:1px solid var(--line);border-radius:22px;
padding:26px 28px;box-shadow:var(--shadow)}
.facts h3{margin:0 0 14px;font-size:14px;text-transform:uppercase;letter-spacing:.08em;
color:var(--muted)}
.facts dl{margin:0;display:grid;gap:16px}
.facts dt{font-size:13px;color:var(--muted)}
.facts dd{margin:2px 0 0;font-weight:600}
main .wrap{padding-top:28px;padding-bottom:96px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:20px;
padding:26px 26px 22px;box-shadow:var(--shadow);transition:transform .2s ease}
.card:hover{transform:translateY(-3px)}
.icon{display:grid;place-items:center;width:44px;height:44px;border-radius:14px;
background:rgba(var(--brand-rgb),.12);color:var(--brand);margin-bottom:14px}
@media (prefers-color-scheme:dark){.icon{color:var(--ink);background:rgba(var(--brand-rgb),.35)}}
.card h2{font-size:18px;margin:0 0 8px;letter-spacing:-.01em}
.card p{margin:0 0 8px;color:var(--muted)}.card ul{margin:0;padding-left:20px;color:var(--muted)}
.card table{border-collapse:collapse;color:var(--muted)}
.card th{text-align:left;padding:3px 18px 3px 0;font-weight:600;color:var(--ink)}
.more{display:inline-block;margin-top:6px;font-weight:600;color:var(--brand);
text-decoration:none}
@media (prefers-color-scheme:dark){.more{color:var(--ink)}}
.band{margin:0 auto 80px;max-width:1080px;border-radius:28px;padding:44px 40px;
background:linear-gradient(135deg,var(--brand),
color-mix(in srgb,var(--brand) 55%,var(--accent)));
color:var(--on-brand);display:flex;align-items:center;justify-content:space-between;gap:24px;
flex-wrap:wrap;box-shadow:var(--shadow)}
.band h2{margin:0 0 6px;font-size:clamp(24px,3.4vw,32px);letter-spacing:-.02em}
.band p{margin:0;opacity:.9}
.band .btn{background:var(--on-brand);color:var(--brand);border-color:var(--on-brand)}
footer{border-top:1px solid var(--line);color:var(--muted);font-size:14px}
footer .wrap{padding-top:22px;padding-bottom:30px;display:flex;justify-content:space-between;
gap:12px;flex-wrap:wrap}
#bubble{position:fixed;right:20px;bottom:20px;z-index:10;box-shadow:0 10px 30px rgba(0,0,0,.2)}
#panel{position:fixed;right:20px;bottom:88px;width:390px;height:min(620px,calc(100vh - 120px));
border:1px solid var(--line);border-radius:20px;overflow:hidden;background:var(--panel);
box-shadow:0 18px 50px rgba(0,0,0,.25);z-index:10;display:none}
#panel.open{display:block}#panel iframe{width:100%;height:100%;border:0}
@media (max-width:560px){nav .btn{display:none}}
@media (max-width:820px){.hero .wrap{grid-template-columns:1fr;gap:28px;padding-top:56px;
padding-bottom:56px}.band{margin:0 16px 64px;padding:32px 24px}}
@media (prefers-reduced-motion:no-preference){.card,.facts{animation:rise .5s ease both}
.card:nth-child(2){animation-delay:.05s}.card:nth-child(3){animation-delay:.1s}
.card:nth-child(4){animation-delay:.15s}.card:nth-child(n+5){animation-delay:.2s}}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
</style>
</head>
<body>
<nav><div class="wrap">
<a class="brand" href="#">__LOGO__<span class="name">__NAME__</span></a>
<a class="btn primary small" href="__CHAT_URL__" data-chat>__CHAT__</a>
</div></nav>
<header class="hero"><div class="wrap">
<div>
__EYEBROW__
<h1>__NAME__</h1>
<div class="intro">__INTRO__</div>
<div class="actions">
<a class="btn primary" href="__CHAT_URL__" data-chat>__CHAT__</a>
__WHATSAPP__
</div>
</div>
__FACTS__
</div></header>
<main><div class="wrap"><div class="grid">__CARDS__</div></div></main>
<section class="band">
<div><h2>__BAND__</h2><p>__BAND_SUB__</p></div>
<a class="btn" href="__CHAT_URL__" data-chat>__CHAT__</a>
</section>
<footer><div class="wrap"><span>© __NAME__</span><span>__NOTE__</span></div></footer>
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
