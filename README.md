# dif-general-harness

**The general solution template behind Di-Factory's agentic solutions.**
One open-source agent runtime, many client solutions: each one is a
declarative *solution spec* on top of a shared core.

> **Status: design phase.** The architecture and PRD are approved; no code has
> been written yet. Implementation starts with milestone M0 (due 2026-10-23).
> Commands and APIs described below are the planned interface.

---

## Why

[Di-Factory](https://di-factory.biz) (∂i~ƒ, Data Intelligence Factory) builds
AI solutions at a fixed price and on a fixed timeline across seven lines of
business: PyME/SMB solutions, Agentic Transformation cells, OPC Startup, ML
models, MVPs, consulting and VC partnerships.

Most of those solutions need the same machinery: an agent loop, tools,
channels, memory, safety, compliance and deployment. Rebuilding it for every
client is slow and fragile. `dif-general-harness` builds it **once**:

- **80% template:** core runtime, capability modules, governance and
  operations, maintained here.
- **20% instance:** a solution spec with the client's prompts, rules,
  connectors and data.

Onboarding a client means writing a spec and connecting secrets, not writing
a new codebase.

## Principles

| Principle | What it means in the code |
|---|---|
| **Client owns everything** | Deploys into the client's cloud with client-owned keys; the solution keeps running without Di-Factory. |
| **No lock-in** | MIT license. Model, provider and cloud are configuration, and swapping them is a spec change validated by evals. |
| **Tools are contracts** | Every capability is a JSON-schema tool (Python, MCP or HTTP). Adding one is a registry entry, not a redeploy. |
| **Effects are verified** | Every side effect passes permissions and, when critical, verification before it commits. Risky actions wait for human approval. |
| **Governance built in** | PII tokenization before the model, consent and opt-out, an immutable audit log and retention, targeting Mexico's LFPDPPP first. |
| **Zero markup** | Costs are tracked per tenant, vendor and role, so every third-party cost is visible. |

## Architecture at a glance

```
Surfaces     channels & triggers · admin / approvals API · TUI console · Python API
Instance     solution spec  →  tenant-scoped instance
Core         agent loop · prompt builder · context manager · permissions
             guardrails & budgets · verifier · workflow engine
Modules      providers · tools + MCP · knowledge (RAG) · 5-layer memory
             agent teams · HITL inbox · ML-model tools · evals
Governance   PII tokens · consent · audit log · retention · region policy
Platform     storage (SQLite / Postgres) · secrets vault · executor · deploy (Docker, Terraform)
```

We own the agent loop instead of wrapping a framework, so context handling,
safety and cost stay visible and cheap to change. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design and all 28
recorded decisions.

## What you can build with it

| Line of business | Delivered as |
|---|---|
| PyME / SMB Solutions | Packs such as Appointment, Lead Qualification, Receipt Processing, Service Reminder, Churn and Recommendation agents |
| Agentic Transformation | Dev, Ops and Service Desk cells as agent teams |
| Conversational RAG | Cited answers from client documents over WhatsApp, Telegram, web or email |
| ML Models | Trained models exposed to agents as tools |
| MVP, Consulting, VC | Delivery agents that speed up Di-Factory's fixed-price engagements |
| OPC Startup | An AI C-suite roster, when the client chooses this runtime |

Packs follow an **open-core** model: the core and reference packs are MIT;
production packs are Di-Factory assets; each client fully owns its instance.

## A solution spec (planned shape)

```jsonc
{
  "solution": { "id": "clinic-appointments", "version": "1.0.0", "lob": "pyme", "locale": "es-MX" },
  "extends": ["packs/pyme-appointment-agent@1"],
  "models":  { "main": { "provider": "anthropic", "model": "<model-id>" } },
  "channels": [{ "type": "gateway", "provider": "twilio", "secret": "twilio-creds" }],
  "tools":   { "packs": ["connectors/google-calendar"] },
  "triggers": [{ "type": "schedule", "cron": "0 9 * * *", "workflow": "send-reminders" }],
  "governance": { "pii": ["name", "phone", "curp"], "retention_days": 90, "consent": "required" },
  "policies": { "profile": "strict", "budgets": { "usd_per_day": 5 } }
}
```

## Roadmap

| Milestone | Due | Scope |
|---|---|---|
| **M0 Core** | 2026-10-23 | Data model, event stream, agent loop, solution-spec schema, tenant ids, offline tests |
| **M1 Agent core** | 2026-11-20 | Anthropic + OpenAI-compatible providers, tool registry, MCP/HTTP tools, permissions, budgets, TUI console |
| **M2 Runtime** | 2026-12-18 | Headless service, channels, triggers, durable queue, approvals inbox, PII/consent/audit, Postgres |
| **M3 Intelligence** | 2027-01-15 | 5-layer memory, knowledge/RAG, verification, agent teams, feedback loop, pack evals |
| **M4 Operations** | 2027-02-12 | Terraform (AWS), OpenTelemetry, cost reports, region policy → **v1.0** |

Each milestone ships as a PyPI pre-release once its acceptance gate passes.

## Getting started (planned)

```bash
uv tool install di-factory-general-harness       # after the first release
dif-general-harness new my-solution --pack pyme-appointment-agent
dif-general-harness console my-solution          # run locally with a fake or real model
dif-general-harness deploy my-solution --target aws
```

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Technical design: framing, modules, all subsystems, stack, layout, roadmap, decisions |
| [`docs/PRD.md`](docs/PRD.md) | Product requirements: goals, personas, user stories, requirements, metrics, milestones, risks |

## Tech stack

Python 3.12+ · uv · Pydantic v2 · FastAPI · Textual · PostgreSQL + pgvector ·
SQLite · official `mcp` SDK · Anthropic and OpenAI-compatible SDKs · Docker ·
Terraform · pytest · ruff · mypy

## License

[MIT](LICENSE) © 2026 Data Intelligence Factory
