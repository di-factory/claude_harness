# OPC C-suite: a team of AI executives for a one-person company

**Status: the live test kit is ready and its flows pass offline; not yet run live.** Pack:
[`opc-c-suite`](../spec/examples/opc-c-suite/pack.json).

## What it is

For a founder who runs a company alone: three long-lived AI executives with their own jobs,
schedules and budgets, sharing a task ledger, delegating to each other and asking the
founder for decisions over Telegram.

| Executive | Job | Works |
|---|---|---|
| **CGO** (growth) | content, social posts, leads, pipeline, with a content writer under it | Mon, Wed, Fri at 11:00, and when given a task |
| **COO** (operations) | tasks, processes, scheduling, quality; runs the daily stand-up | daily, at the founder's chosen time |
| **CTO** (technology) | monitoring, alerts, backups of the product | every 30 minutes |

## The story for a client

You are the whole company. Your executives keep the routine going: every morning a stand-up
summary on Telegram (what each did, what is blocked, what needs you), posts drafted on
schedule, the product watched every half hour, leads followed up. Nothing is published, sent
or changed in your CRM without your OK on Telegram; you decide, they execute.

## How it works

- **The ledger** is the team's shared task list (title, owner, status, due date, priority,
  "needs the founder"). Assigning a task to an executive wakes that executive.
- **The daily stand-up** runs the three updates in parallel, writes one brief and sends it
  to the founder.
- **Schedules** wake each executive directly with its standing order.

## The safeguards

- **Publishing a post, sending an email from the CRM and updating the CRM ask the founder**
  (on Telegram, 12 h, then rejected); a second model also reviews posts and emails.
- **Deleting in the CRM is denied;** the CTO only reads metrics and alerts.
- **Tasks marked "needs the founder"** go to the founder's Telegram.
- **Each executive has its own budget;** the whole team USD 12 per day.

## Preparing the kit (November 2026)

Reading the code before the first live run found what would have broken it (decision 100):

- **Anyone could talk to the team.** The founder's Telegram bot answered every chat, with
  ledger write access, and nothing registered its webhook. Now an operator channel (founder,
  staff) answers only its own chat, and `serve --public-url` registers the webhook itself.
- **The founder could not approve from the chat**, and each notice said only "Approval
  needed: social.publish_post", twice. Now it says what would be done, once, and the founder
  replies `approve ID` or `reject ID why`; after an approval he is told the result.
- **Every post from a schedule or a task would have been blocked** by the reviewing model,
  which asked whether "the contact" had requested it. A run with no contact is now judged
  against its task.
- **The CTO had no metrics**: its connection used an authentication the harness does not have
  (`spec validate` now refuses that). It now reads a monitoring API with a token.
- **The stand-up brief never saw the CGO's and CTO's updates**, and the founder would have read
  `<NAME_…>` tokens instead of names. Both fixed.
- **The ledger:** an executive giving itself a task woke itself (a loop every half hour for the
  CTO); a task that needs the founder paged him again on every update; tasks could be given to
  the content writer, who cannot read the ledger. All fixed.

## How to try it

Follow [`docs/testing/opc/README.md`](../testing/opc/README.md): a Telegram bot from
@BotFather, stand-ins for the CRM, the monitoring and LinkedIn on the server, and eight
scenarios (the chat, a stranger ignored, the stand-up, a post approved from the chat, a post
rejected, a CTO alert, a CRM email). For a real client it needs the client's CRM with an MCP
server, a LinkedIn connection that posts to the company page, and a monitoring API.
