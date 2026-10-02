"""The web chat: a page the instance serves itself (``GET /chat``), for the client's website.

The page posts ``{"contact": "<browser id>", "text": "..."}`` to ``POST /channels/{name}``
and shows the reply that comes back; replies sent later (a person answering from the inbox,
a reminder) wait in a small outbox the page polls (``GET /channels/{name}/outbox``).

Two modes:
- **private** (default): every request carries ``Authorization: Bearer <token>``, the
  channel's credentials; the page asks for that access code once. Without credentials the
  channel refuses everything rather than run open.
- **public** (``"public": true``): anyone with the link can chat. The browser's id is a
  random 128-bit value, so one visitor cannot read another's conversation; messages are
  rate-limited per visitor and per address, and capped in length. The instance's budgets
  (``policies.budgets``) still bound what the model can spend.

The outbox lives in memory: a restart loses replies nobody has fetched yet (the
conversation itself is stored, and a person sees it in the inbox).
"""

from __future__ import annotations

import hmac
import re
import time
from collections import defaultdict, deque
from typing import Any

from ..spec.schema import Channel
from .base import ChannelError, Envelope, Inbound, Unauthorized

_VISITOR = re.compile(r"^[A-Za-z0-9-]{16,64}$")
MAX_CHARS = 2000
PER_VISITOR = (12, 60.0)  # messages per seconds
PER_ADDRESS = (40, 600.0)
OUTBOX_KEEP = 50


class RateLimited(ChannelError):
    """Too many messages from one visitor or address; try again shortly."""


class WebChannel:
    inline_reply = True
    outbound = True  # replies sent later wait in the outbox

    def __init__(self, name: str, config: Channel, credentials: Any) -> None:
        self.name = name
        self.config = config
        self.token = credentials if isinstance(credentials, str) and credentials else None
        self.public = config.public
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._outbox: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=OUTBOX_KEEP))

    # --- checks ---------------------------------------------------------------------

    def _authorize(self, request: Inbound) -> None:
        if self.public:
            return
        auth = request.headers.get("authorization", "")
        given = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if self.token is None or not given or not hmac.compare_digest(given, self.token):
            raise Unauthorized("invalid or missing access code")

    def _visitor(self, raw: Any) -> str:
        if not isinstance(raw, str) or not _VISITOR.match(raw):
            raise ChannelError("expected a 'contact' id of 16-64 letters, digits or dashes")
        return f"web-{raw}"

    def _limit(self, key: str, rule: tuple[int, float], now: float) -> None:
        count, window = rule
        hits = self._hits[key]
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= count:
            raise RateLimited("too many messages; wait a moment and try again")
        hits.append(now)

    @staticmethod
    def address(request: Inbound) -> str:
        """The sender's address: the proxy's X-Forwarded-For (its last hop, which the proxy
        itself wrote), else the peer."""
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        return hops[-1] if hops else request.client or "unknown"

    # --- the adapter ----------------------------------------------------------------

    def parse(self, request: Inbound) -> list[Envelope]:
        self._authorize(request)
        body = request.json()
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise ChannelError("expected JSON with 'contact' and 'text'")
        contact = self._visitor(body.get("contact"))
        text = body["text"].strip()
        if not text:
            raise ChannelError("the message is empty")
        if len(text) > MAX_CHARS:
            raise ChannelError(f"the message is longer than {MAX_CHARS} characters")
        if self.public:
            now = time.monotonic()
            self._limit(f"v:{contact}", PER_VISITOR, now)
            self._limit(f"a:{self.address(request)}", PER_ADDRESS, now)
        return [Envelope(channel=self.name, contact_key=contact, text=text)]

    def poll(self, request: Inbound, contact: str) -> list[str]:
        """Replies that arrived after the page's last request (each is given once)."""
        self._authorize(request)
        box = self._outbox.get(self._visitor(contact))
        if not box:
            return []
        out = list(box)
        box.clear()
        return out

    async def send(self, contact_key: str, text: str) -> str | None:
        self._outbox[contact_key].append(text)
        return None

    async def send_template(
        self, contact_key: str, template: str, variables: dict[str, str]
    ) -> str | None:
        return None  # templates are for WhatsApp's 24-hour rule; the web has none
