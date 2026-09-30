"""Voice (Twilio Programmable Voice): phone calls with the same agents, turn by turn.

The phone number's voice webhook points at ``/channels/<name>``. Twilio recognizes speech
itself (``<Gather input="speech">``), so the harness only ever sees text:

1. a call arrives (no ``SpeechResult``): the answer is the greeting inside a speech gather;
2. each thing the caller says comes back as ``SpeechResult``: it is one ordinary inline turn
   (consent, escalation, the intent router, budgets and audit all apply), and the agent's
   reply is spoken inside the next gather, so the conversation continues;
3. silence ends the call politely; an escalated turn transfers the call to ``transfer_to``
   (a person's number) when one is set, and otherwise says goodbye;
4. Twilio's status callbacks (``CallStatus`` completed, busy...) get an empty answer.

Twilio waits about 15 seconds for a webhook, so voice agents should be quick: a fast main
model, few tool calls, and the router short-circuit for small talk.

Requests are verified like the messaging gateway (``X-Twilio-Signature`` over the public
URL). Replies are made speakable: citation markers and the Sources list are dropped,
Markdown is flattened. Outbound (``send``) places a call that speaks the text, for example a
reminder, and goes through consent like any message the harness starts.

Settings (``channels.<name>.voice``): ``language`` (default ``en-US``; ``es-MX`` for
Mexico), ``voice`` (a Twilio/Polly voice name), ``greeting``, ``goodbye``, ``transfer_to``,
``speech_timeout`` (default ``auto``) and ``hints`` (words to help recognition).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import httpx2

from ..spec.schema import Channel
from .base import ChannelError, Envelope, Inbound, Unauthorized, credential_pair
from .gateway import API, signature

ENDED = {"completed", "busy", "failed", "no-answer", "canceled"}
DEFAULTS = {
    "en": ("Hello, how can I help you?", "Thank you for calling. Goodbye.",
           "Let me transfer you to a person."),
    "es": ("Hola, ¿en qué le puedo ayudar?", "Gracias por llamar. Hasta luego.",
           "Le comunico con una persona."),
}  # fmt: skip
_CITATION = re.compile(r"\s*\[\d+\]")
_MARKDOWN = re.compile(r"[*_`#>]+")


def speakable(text: str) -> str:
    """What a voice can say: no citation markers, no Sources list, no Markdown."""
    kept = []
    for line in text.splitlines():
        if line.strip().lower() in ("sources:", "fuentes:"):
            break  # the list of sources is for screens
        kept.append(line)
    spoken = _MARKDOWN.sub("", _CITATION.sub("", "\n".join(kept)))
    spoken = re.sub(r"https?://\S+", "", spoken)
    return re.sub(r"\s+", " ", spoken).strip()


class VoiceChannel:
    inline_reply = True  # answers go back in the webhook's TwiML
    outbound = True  # and the harness can also start a call

    def __init__(
        self,
        name: str,
        config: Channel,
        credentials: Any,
        *,
        client: httpx2.AsyncClient | None = None,
        public_url: str | None = None,
    ) -> None:
        if config.provider not in {None, "twilio"}:
            raise ChannelError(f"voice provider {config.provider!r} is not supported yet")
        self.name = name
        self.config = config
        self.account_sid, self.auth_token = credential_pair(
            credentials, "account_sid", "auth_token"
        )
        self.http = client or httpx2.AsyncClient(timeout=20.0)
        self.public_url = public_url
        self.settings: dict[str, Any] = dict(config.voice or {})
        self.language = str(self.settings.get("language") or "en-US")
        greeting, goodbye, transfer = DEFAULTS.get(self.language[:2], DEFAULTS["en"])
        self.greeting = str(self.settings.get("greeting") or greeting)
        self.goodbye = str(self.settings.get("goodbye") or goodbye)
        self.transferring = str(self.settings.get("transferring") or transfer)

    # --- inbound -----------------------------------------------------------------------

    def _verify(self, request: Inbound) -> dict[str, str]:
        params = request.form()
        url = self.public_url or request.url
        given = request.headers.get("x-twilio-signature", "")
        if not given or not hmac.compare_digest(given, signature(self.auth_token, url, params)):
            raise Unauthorized("invalid voice signature")
        return params

    def parse(self, request: Inbound) -> list[Envelope]:
        params = self._verify(request)
        speech = params.get("SpeechResult", "").strip()
        caller = params.get("From", "")
        if not speech or not caller or params.get("CallStatus") in ENDED:
            return []  # a new call, silence, or a status callback: no turn
        turn = hashlib.sha256(speech.encode()).hexdigest()[:12]
        return [Envelope(channel=self.name, contact_key=caller, text=speech,
                         message_id=f"{params.get('CallSid', '')}:{turn}")]  # fmt: skip

    def respond(self, request: Inbound, results: list[Any]) -> tuple[str, str]:
        """The TwiML for this webhook: greet, answer and listen again, transfer or hang up."""
        params = request.form()
        if params.get("CallStatus") in ENDED:
            return "<Response/>", "application/xml"
        if not results:
            if "SpeechResult" in params:  # the caller said nothing we could recognize
                return self._twiml(self._say(self.goodbye), "<Hangup/>"), "application/xml"
            return self._twiml(self._gather(self.greeting), self._say(self.goodbye),
                               "<Hangup/>"), "application/xml"  # fmt: skip
        spoken = " ".join(speakable(r.reply) for r in results if r.reply)
        if any(r.escalated for r in results):
            to = self.settings.get("transfer_to")
            if to:
                parts = [self._say(spoken)] if spoken else []
                parts += [self._say(self.transferring), f"<Dial>{escape(str(to))}</Dial>"]
                return self._twiml(*parts), "application/xml"
            return self._twiml(self._say(spoken or self.goodbye), "<Hangup/>"), "application/xml"
        return self._twiml(self._gather(spoken or "…"), self._say(self.goodbye),
                           "<Hangup/>"), "application/xml"  # fmt: skip

    def _say(self, text: str) -> str:
        attrs = f" language={quoteattr(self.language)}"
        if self.settings.get("voice"):
            attrs += f" voice={quoteattr(str(self.settings['voice']))}"
        return f"<Say{attrs}>{escape(text)}</Say>"

    def _gather(self, text: str) -> str:
        attrs = {
            "input": "speech",
            "method": "POST",
            "language": self.language,
            "speechTimeout": str(self.settings.get("speech_timeout") or "auto"),
        }
        if self.public_url:
            attrs["action"] = self.public_url
        if self.settings.get("hints"):
            attrs["hints"] = ", ".join(str(h) for h in self.settings["hints"])
        rendered = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())
        return f"<Gather {rendered}>{self._say(text)}</Gather>"

    def _twiml(self, *parts: str) -> str:
        return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(parts) + "</Response>"

    # --- outbound ----------------------------------------------------------------------

    async def send(self, contact_key: str, text: str) -> str | None:
        """Call the contact and speak ``text`` (a reminder, a follow-up)."""
        if not self.config.address:
            raise ChannelError(f"voice channel {self.name!r} needs an address to call from")
        response = await self.http.post(
            f"{API}/Accounts/{self.account_sid}/Calls.json",
            data={
                "From": self.config.address,
                "To": contact_key,
                "Twiml": self._twiml(self._say(speakable(text))),
            },
            auth=(self.account_sid, self.auth_token),
        )
        if response.status_code >= 400:
            raise ChannelError(f"voice call failed: HTTP {response.status_code}")
        sid = response.json().get("sid")
        return str(sid) if sid else None

    async def send_template(
        self, contact_key: str, template: str, variables: dict[str, str]
    ) -> str | None:
        """Calls have no approved templates: the template's text is spoken."""
        tpl = self.config.templates.get(template)
        if tpl is None:
            raise ChannelError(f"unknown template {template!r}")
        from pathlib import Path

        text = Path(tpl.file).read_text(encoding="utf-8")
        for key, value in variables.items():
            text = text.replace("{{" + key + "}}", value)
        return await self.send(contact_key, text)
