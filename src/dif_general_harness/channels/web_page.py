"""The chat page the web channel serves: one self-contained HTML file, no outside requests."""

from __future__ import annotations

import html
import json
import re

TEXTS = {
    "es": {
        "hello": "¡Hola! ¿En qué te puedo ayudar?",
        "placeholder": "Escribe tu mensaje…",
        "send": "Enviar",
        "code": "Código de acceso",
        "wait": "Escribiendo…",
        "error": "No se pudo enviar. Intenta de nuevo.",
        "busy": "Demasiados mensajes seguidos; espera un momento.",
        "person": "Una persona del equipo te responderá aquí.",
        "note": "Asistente automático; una persona revisa cuando hace falta.",
        "new": "Nueva conversación",
        "home": "Volver al sitio",
        "private": "Este chat es privado: escribe el código de acceso que te dieron.",
        "wrong": "Ese código de acceso no funcionó; revísalo e intenta de nuevo.",
        "enter": "Entrar",
    },
    "en": {
        "hello": "Hi! How can I help you?",
        "placeholder": "Type your message…",
        "send": "Send",
        "code": "Access code",
        "wait": "Typing…",
        "error": "Could not send. Please try again.",
        "busy": "Too many messages in a row; wait a moment.",
        "person": "A person from the team will answer here.",
        "note": "Automated assistant; a person reviews when needed.",
        "new": "New conversation",
        "home": "Back to the site",
        "private": "This chat is private: type the access code you were given.",
        "wrong": "That access code did not work; check it and try again.",
        "enter": "Enter",
    },
}

_PAGE = r"""<!doctype html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f6f3;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e3de;
--me:#1f5f4a;--me-ink:#fff;--bot:#efefea;--accent:#1f5f4a}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1d1d1b;--ink:#ecece8;
--muted:#9a9a94;--line:#2e2e2b;--me:#3f8f74;--me-ink:#fff;--bot:#2a2a27;--accent:#5fb394}}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif;display:flex;justify-content:center}
.app{width:100%;max-width:720px;height:100%;height:100dvh;display:flex;
flex-direction:column;
background:var(--panel);border-left:1px solid var(--line);border-right:1px solid var(--line)}
header{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;
align-items:center;justify-content:space-between;gap:12px}
#home{font-size:20px;line-height:1;text-decoration:none;color:var(--muted);padding:4px 6px;
border-radius:8px}#home:hover{color:var(--ink);background:var(--line)}
header h1{font-size:17px;margin:0;font-weight:600;flex:1;min-width:0;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.mark{height:30px;width:auto;max-width:90px;object-fit:contain;border-radius:6px}
header button{background:none;border:1px solid var(--line);color:var(--muted);
border-radius:8px;padding:6px 10px;font:inherit;font-size:13px;cursor:pointer}
#log{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:85%;padding:10px 13px;border-radius:14px;white-space:pre-wrap;
overflow-wrap:anywhere}
.bot{white-space:normal}.bot ul{margin:4px 0;padding-left:20px}.bot li{margin:2px 0}
.bot{align-self:flex-start;background:var(--bot);border-bottom-left-radius:4px}
.me{align-self:flex-end;background:var(--me);color:var(--me-ink);border-bottom-right-radius:4px}
.info{align-self:center;color:var(--muted);font-size:13px;text-align:center}
form{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--line)}
textarea{flex:1;min-width:0;resize:none;border:1px solid var(--line);border-radius:12px;
padding:10px 12px;font:inherit;background:var(--panel);color:var(--ink);max-height:140px}
textarea:focus{outline:2px solid var(--accent);outline-offset:-1px}
form button{border:0;border-radius:12px;padding:0 18px;background:var(--accent);color:#fff;
font:inherit;font-weight:600;cursor:pointer}
form button:disabled{opacity:.5;cursor:default}
[hidden]{display:none!important}
#gate{flex-wrap:wrap}#gate p{flex-basis:100%;margin:0 0 4px;color:var(--muted);font-size:14px}
#gate input{flex:1;min-width:0;border:1px solid var(--line);border-radius:12px;
padding:10px 12px;font:inherit;background:var(--panel);color:var(--ink)}
footer{padding:0 16px 10px;color:var(--muted);font-size:12px;text-align:center}
</style>
</head>
<body>
<div class="app">
<header><a id="home" href="/" target="_top" aria-label="__HOME__">←</a>
<h1>__TITLE__</h1><button id="new" type="button"></button></header>
<div id="log" aria-live="polite"></div>
<form id="gate" hidden><p id="gatemsg"></p>
<input id="code" type="password" autocomplete="off"><button id="enter" type="submit"></button>
</form>
<form id="form"><textarea id="text" rows="1" maxlength="2000"></textarea>
<button id="send" type="submit"></button></form>
<footer id="note"></footer>
</div>
<script>
const CFG = __CONFIG__;
const T = CFG.texts;
const $ = (id) => document.getElementById(id);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
  del(k) { try { localStorage.removeItem(k); } catch (e) {} },
};
const KEY = "dif-chat-" + CFG.channel;
function newId() {
  const b = new Uint8Array(16); crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
let visitor = store.get(KEY + "-id") || newId();
store.set(KEY + "-id", visitor);
let code = store.get(KEY + "-code") || "";
function headers() {
  const h = {"Content-Type": "application/json"};
  if (!CFG.public && code) h.Authorization = "Bearer " + code;
  return h;
}
function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function inline(s) {  // on escaped text: **bold**, *italic*
  return s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*?)\*(?!\*)/g, "$1<em>$2</em>");
}
function markdown(text) {  // the agent's replies: paragraphs, lists, bold; nothing else
  const out = []; let list = null;
  for (const raw of text.split("\n")) {
    const line = esc(raw.trim()); const item = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (item) { if (!list) { list = []; out.push(list); } list.push(inline(item[1])); continue; }
    list = null; out.push(line ? inline(line.replace(/^#{1,6}\s+/, "")) : "");
  }
  return out.map((b) => Array.isArray(b) ? "<ul>" + b.map((i) => "<li>" + i + "</li>").join("")
    + "</ul>" : b).join("<br>").replace(/(<br>){3,}/g, "<br><br>")
    .replace(/<br>(<ul>)/g, "$1").replace(/(<\/ul>)<br>/g, "$1");
}
function add(kind, text) {
  const div = document.createElement("div");
  div.className = "msg " + kind;
  if (kind === "bot") div.innerHTML = markdown(text); else div.textContent = text;
  $("log").appendChild(div); $("log").scrollTop = $("log").scrollHeight;
  return div;
}
function history() {
  try { return JSON.parse(store.get(KEY + "-log") || "[]"); } catch (e) { return []; }
}
function remember(kind, text) {
  const h = history(); h.push([kind, text]); store.set(KEY + "-log", JSON.stringify(h.slice(-100)));
}
function say(kind, text) { add(kind, text); remember(kind, text); }
if (window.top !== window) $("home").style.display = "none";  // inside the site's panel
$("send").textContent = T.send; $("text").placeholder = T.placeholder;
$("note").textContent = T.note; $("new").textContent = T.new;
const past = history();
if (past.length) past.forEach(([k, t]) => add(k, t)); else add("bot", T.hello);
function gate(message) {  // a private chat: the access code, asked on the page itself
  $("form").hidden = true; $("gate").hidden = false;
  $("gatemsg").textContent = message; $("code").value = ""; $("code").focus();
}
$("enter").textContent = T.enter; $("code").placeholder = T.code;
$("gate").onsubmit = (e) => {
  e.preventDefault();
  code = $("code").value.trim(); if (!code) return;
  store.set(KEY + "-code", code);
  $("gate").hidden = true; $("form").hidden = false; $("text").focus();
};
if (!CFG.public && !code) gate(T.private);
$("new").onclick = () => {
  store.del(KEY + "-log"); visitor = newId(); store.set(KEY + "-id", visitor);
  $("log").innerHTML = ""; add("bot", T.hello);
};
$("text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("form").requestSubmit(); }
});
$("form").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("text").value.trim(); if (!text) return;
  $("text").value = ""; say("me", text); $("send").disabled = true;
  const wait = add("info", T.wait);
  try {
    const r = await fetch(CFG.post, {method: "POST", headers: headers(),
      body: JSON.stringify({contact: visitor, text})});
    wait.remove();
    if (r.status === 401) {
      store.del(KEY + "-code"); code = ""; $("text").value = text; gate(T.wrong); return;
    }
    if (r.status === 429) { add("info", T.busy); return; }
    if (!r.ok) { add("info", T.error); return; }
    const data = await r.json();
    for (const reply of data.replies || []) {
      if (reply.reply) say("bot", reply.reply);
      else add("info", T.person);  // held for a person, or handed off
    }
  } catch (err) { wait.remove(); add("info", T.error); }
  finally { $("send").disabled = false; $("text").focus(); }
};
async function poll() {
  try {
    const r = await fetch(CFG.outbox + "?contact=" + encodeURIComponent(visitor),
      {headers: headers()});
    if (r.ok) for (const text of (await r.json()).messages || []) say("bot", text);
  } catch (e) {}
}
setInterval(poll, 8000);
</script>
</body>
</html>
"""


def render_chat(
    channel: str, title: str, *, locale: str | None, public: bool,
    color: str | None = None, logo: str | None = None, icon: str | None = None,
) -> str:  # fmt: skip
    """The chat page; ``color`` and ``logo`` come from the instance's branding."""
    texts = TEXTS.get((locale or "en")[:2], TEXTS["en"])
    config = {
        "channel": channel,
        "public": public,
        "post": f"/channels/{channel}",
        "outbox": f"/channels/{channel}/outbox",
        "texts": texts,
    }
    # "</" cannot appear inside the script block, whatever the title or texts hold
    script = json.dumps(config, ensure_ascii=False).replace("</", "<\\/")
    brand = ""
    if color and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        from .landing import _on

        brand = f"<style>:root{{--me:{color};--me-ink:{_on(color)};--accent:{color}}}</style>"
    mark = ""
    if logo and logo.startswith("data:image/"):
        mark = f'<img class="mark" src="{html.escape(logo)}" alt="">'
    return (
        _PAGE.replace("__LANG__", html.escape((locale or "en")[:2]))
        .replace(
            "</style>\n</head>",
            "</style>\n" + brand + (f'<link rel="icon" href="{icon}">' if icon else "") + "</head>",
            1,
        )
        .replace("<h1>__TITLE__", f"{mark}<h1>__TITLE__", 1)
        .replace("__HOME__", html.escape(texts["home"]))
        .replace("__TITLE__", html.escape(title))
        .replace("__CONFIG__", script)
    )
