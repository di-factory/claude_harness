# OPC C-suite: a team of AI executives for a one-person company

**Status: designed and checked on paper and in offline tests; not yet run live.** Pack:
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

## Before a live test

It needs a CRM with an MCP server, a LinkedIn connection, read-only cloud metrics and the
founder's Telegram. It has the most moving parts of all the packs; a test kit with stand-ins
(a fake CRM and product metrics) is the way to start.
