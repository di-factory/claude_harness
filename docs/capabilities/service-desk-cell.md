# Service Desk cell: Tier-1 IT tickets resolved around the clock

**Status: designed and checked on paper and in offline tests; not yet run live.** Pack:
[`service-desk-cell`](../spec/examples/service-desk-cell/pack.json).

## What it is

A team of three agents that works the client's helpdesk: a **triage** agent classifies each
new ticket and sets its priority, a **resolver** solves Tier-1 requests (password resets,
known how-tos) following the company's runbooks, and a **knowledge researcher** finds the
right runbook steps and cites them. Anything beyond Tier 1, urgent or unverified goes to the
IT team in the helpdesk itself, and an SLA timer escalates a ticket nobody answered in time.

## The story for a client

Half your IT tickets are the same few requests: "I forgot my password", "how do I connect to
the VPN". The cell answers them at 3 a.m. as well as at noon, following your own runbooks and
documenting every step on the ticket. It only resets a password after confirming who is
asking; important accounts and urgent tickets always go to your team; and if a ticket is not
solved within your first-response time, your team is told.

## How it works

1. The helpdesk calls the cell's webhook when a ticket is created (signed with a shared secret).
2. The `resolve-ticket` workflow starts the SLA timer, then **triage** classifies the ticket.
3. The route: Tier 1 → the **resolver** (with the **researcher** for the runbook steps);
   anything else → handed to the escalation group in the helpdesk.
4. The SLA timer escalates the ticket and tells the team on Slack if it is still open.

## The safeguards

- **Identity first:** a check requires a verified contact whose email matches the ticket's
  requester before a password reset.
- **A second model verifies** every solved ticket and every password reset against the
  runbook ("the problem is solved; every action is documented").
- **Resetting an administrator account always asks a person** (on Slack, 1 h, then escalated);
  deleting tickets is denied.
- **P1 and P2 tickets, a contact who asks for a person, and a verification that failed twice**
  go to the team.
- **Budget:** USD 0.50 per ticket, USD 40 per day.

## Before a live test

It needs a helpdesk with an MCP server or API, an identity provider's API, the runbooks
(a Drive folder) and a Slack channel. As with the Dev Cell and the receipts, expect the first
live run to find things the offline tests could not (tool names of the real helpdesk, rules
that never fire); a test kit with stand-ins, like the receipts one, is the way to start.
