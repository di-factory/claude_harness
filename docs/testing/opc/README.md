# OPC C-suite live test kit

A first live test of the `opc-c-suite` pack: a founder who runs a company alone talks to a team
of AI executives on Telegram. The COO answers in the founder's chat and keeps the shared task
ledger; the CGO handles growth (posts, the CRM); the CTO watches the product. Nothing leaves
the company (a post, an email, a CRM change) without the founder's approval, which the founder
gives by replying in the same chat.

The company is made up ("Agenda Fácil", an appointment app). Its CRM, its monitoring and
LinkedIn are stand-ins (`stand_ins.py`): everything the team sends out is written to
`outbox.json`, so you see exactly what would have been published. The same flows run offline in
`tests/test_opc_kit.py`.

| File | What it is |
|---|---|
| [`opc-test.json`](opc-test.json) | the instance: the founder's chat id (you set it), stand-ins on `127.0.0.1:8091`, schedules slowed down (you run them on demand) |
| [`stand_ins.py`](stand_ins.py) | the CRM (a real MCP server, with three contacts), the monitoring API and LinkedIn |
| [`kit.sh`](kit.sh) | `chat-id`, `set-chat`, `caddy-route`, `caddy-restore`, `alert`, `clear-alert`, `outbox` |

## What you need

- The server with the harness (`git pull && uv sync`), HTTPS working (`https://<ip>.sslip.io`)
  and your model key stored as `llm`.
- **A Telegram bot.** In Telegram, talk to **@BotFather**: `/newbot`, any name, a username
  ending in `bot`. It gives you a token like `123456789:AA...`. You type it on the server;
  never paste it here.

## 1. Secrets

```bash
cd ~/claude_harness && git pull && uv sync
uv run dif-general-harness secrets set telegram          # the bot token from BotFather
for s in crm linkedin cloud_readonly; do openssl rand -hex 16 | uv run dif-general-harness secrets set $s; done
uv run dif-general-harness secrets check docs/testing/opc/opc-test.json --packs docs/spec/examples
```

`crm`, `linkedin` and `cloud_readonly` are for the stand-ins, which only check that a token is
sent.

## 2. Your chat id

Only your chat is answered: anyone else who finds the bot is ignored. In Telegram, open your
bot and write it anything ("hola"). Then:

```bash
K=docs/testing/opc/kit.sh
$K chat-id                 # prints: 123456789  Your Name
$K set-chat 123456789      # writes it into opc-test.json
```

## 3. A public path for the bot

Telegram only calls https addresses, and on this server Caddy sends everything to the client on
8080. This adds `https://<host>/opc/` for the OPC agent on 8083, and leaves the client as it is:

```bash
$K caddy-route             # prints the public URL to use below
```

`$K caddy-restore` puts the Caddyfile back when you are done.

## 4. Start (two terminals)

Terminal B, the stand-ins:
```bash
cd ~/claude_harness && uv run python docs/testing/opc/stand_ins.py
```

Terminal C, the team (use the URL `caddy-route` printed):
```bash
cd ~/claude_harness
uv run dif-general-harness serve docs/testing/opc/opc-test.json --packs docs/spec/examples \
  --port 8083 --state ~/opc-state --public-url https://<host>/opc
```

At start it registers the bot's webhook itself. If it cannot, it says why
(`webhook not registered: …`).

In terminal A:
```bash
export DIF_ADMIN_URL=http://localhost:8083
```

## 5. The scenarios, one at a time

| # | Do | Expect |
|---|---|---|
| 1 | In Telegram: "Hola, ¿qué pueden hacer por mí?" | the COO answers in the chat |
| 2 | From **another** Telegram account, write to the bot | no answer at all (only the founder's chat) |
| 3 | `uv run dif-general-harness admin trigger standup` | within a minute or two, one brief in your chat with the CGO's, COO's and CTO's updates |
| 4 | "Que el CGO prepare un post para LinkedIn anunciando los recordatorios por WhatsApp" | the COO confirms; then a message `[inbox ID] Approval needed: social.publish_post … with {"text": …}` and `Reply here: approve ID · reject ID <why>`; nothing in the outbox yet |
| 5 | Reply `approve ID` | `Approved: ID.`, then a message that the post ran; `$K outbox` shows the post |
| 6 | Ask for another post, and reply `reject ID not this week` | `Rejected: ID.`; nothing new in the outbox |
| 7 | `$K alert`, then `uv run dif-general-harness admin trigger cto-health` | the CTO opens a ledger task about the latency; ask the COO "¿qué tareas hay?" to see it |
| 8 | "Escríbele a María Fernández (CRM) que ya tenemos recordatorios por WhatsApp" | the email waits for your approval like the post; approve it and `$K outbox` shows it |

After each one: `uv run dif-general-harness admin runs`, `admin inbox` and `$K outbox`.

## What counts as a pass

- Only your chat is answered; every post and email waited for your `approve` and reached the
  outbox only after it; a `reject` sent nothing.
- The stand-up brief carries all three executives.
- One message per approval or page (not two), with what is to be approved.
- `admin costs` stays within the budget (USD 5 a day in this test).

Anything else is a finding: note the scenario, what happened, `admin runs` and `admin inbox`.

## When something goes wrong

| You see | Likely cause |
|---|---|
| The bot never answers | `webhook not registered` in terminal C (public URL not https, or Caddy route missing: `$K caddy-route`); or the chat id in `opc-test.json` is not yours (`$K chat-id`) |
| `chat-id` prints nothing | write to the bot first; and run it before `serve` registers the webhook (or stop `serve`: `chat-id` removes the webhook to read the messages, the next `serve` sets it again) |
| `tool crm.… is not available`, `mcp_unavailable` | the stand-ins are not running (terminal B) |
| The COO answers "a person will follow up" | the run stopped (budget, an error): `admin runs` and `admin inbox` say why |

## Cleaning up

Stop terminals B and C, `$K caddy-restore`, and if you no longer need the bot, `/deletebot` in
@BotFather.
