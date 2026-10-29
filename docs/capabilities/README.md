# Capabilities: what the harness can build for a client, and how proven each one is

Each solution type is a **pack**: a declarative spec (agents, tools, rules, workflows,
channels) that the harness turns into a running solution for one client. These pages explain
each one in plain terms: what it does, the story for a client, how it works, the safeguards,
and what its tests showed. The technical contract is
[`docs/spec/SOLUTION_SPEC.md`](../spec/SOLUTION_SPEC.md); the design,
[`docs/ARCHITECTURE.md`](../ARCHITECTURE.md).

| Capability | Status | Page |
|---|---|---|
| **Appointment agent**: a receptionist for clinics and practices (FAQ, confirm, reschedule, remind) | ✅ tested live through the web chat; WhatsApp and Google Calendar not yet connected live | [appointment-agent.md](appointment-agent.md) |
| **Conversational assistant**: answers from the client's own documents, with citations | ✅ tested live through the web chat (Di-Factory's own assistant, from di-factory.biz) | [conversational-assistant.md](conversational-assistant.md) |
| **Dev Cell**: an AI developer that turns labelled GitHub issues into tested pull requests | ✅ tested live: 3 of 3 test issues passed (fix, questions, refusal), USD 0.16 | [dev-cell.md](dev-cell.md) |
| **Receipt processing**: invoices read, checked with the SAT and posted to the ERP | 🧪 live test kit ready; the full flow passes offline | [receipt-processing.md](receipt-processing.md) |
| **Service Desk cell**: a team that resolves Tier-1 IT tickets around the clock | 📄 designed, offline tests only | [service-desk-cell.md](service-desk-cell.md) |
| **OPC C-suite**: AI executives (growth, operations, technology) for a one-person company | 📄 designed, offline tests only | [opc-c-suite.md](opc-c-suite.md) |
| **Research graph**: a verified, weekly-updated map of a client's market | 📄 built with offline tests | [research-graph.md](research-graph.md) |

**What "tested live" means here:** set up on a real server with a real model, used the way a
client would, and the result checked. Every first live run so far found problems the offline
tests could not (wrong tool names, rules that never fired, a model parameter one model
refuses), so the untested types should be expected to need the same first pass. A test kit
with stand-ins for the client's systems (like the receipts one) is the way to start.

## What every capability shares

The platform gives every solution the same base, whichever pack it is:

- **Rules outside the model.** What a tool may do (allow, ask a person, deny), budgets per
  run and per day, and checks before an action are enforced by the harness, not by a prompt
  the model could be talked out of.
- **A person decides what matters.** Approvals and handoffs land in an inbox: `admin inbox`
  shows each item with its reason, `admin decide` approves, `admin reply` answers a customer.
- **Personal data is protected.** The model sees names, phones, RFCs as tokens; each tool gets
  in clear only what the pack grants it; replies show only what may be shown.
- **Text from outside is data.** Web pages, documents, issues and tool results are fenced as
  untrusted content, never followed as instructions.
- **Nothing repeated blindly.** An action whose outcome is unknown is checked before it is
  tried again.
- **Measured.** Costs by day, model and client (`admin costs`), why each run stopped
  (`admin runs`), a weekly review that proposes improvements and never applies them itself.
- **Set up in one command, handed over.** `./setup.sh` on a fresh server, fine-tuning rounds,
  then `/handover` leaves the solution with the client's own Claude.
