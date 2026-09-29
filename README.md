# dif-general-harness

**The general solution template behind Di-Factory's agentic solutions:** one of
Di-Factory's base platforms, used to run, adjust and deploy client solutions
across its lines of business.
One open-source agent runtime, many client solutions: each one is a
declarative *solution spec* on top of a shared core.

> **Status: building M0.** The architecture, PRD and solution spec are approved;
> implementation has started with milestone M0 (core skeleton). Commands and APIs
> described below are the planned interface unless marked available.

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

## How a client solution gets built

```
Jag or Teky: "deploy solution S for client X"
  → constructor agent matches S to the pack catalog (no fit → new pack needed)
  → interviews for the open details the pack declares
  → writes the instance spec (the 15–20%), validates it, runs the pack's evals
  → Jag approves
  → deploys into X's own cloud; X keeps its secrets in its own vault
  → instance agent reports to Di-Factory's control plane
```

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
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design and all 40
recorded decisions.

## What you can build with it

| Line of business | Delivered as |
|---|---|
| PyME / SMB Solutions | Packs such as Appointment, Lead Qualification, Receipt Processing, Service Reminder, Churn and Recommendation agents |
| Agentic Transformation | Dev, Ops and Service Desk cells as agent teams |
| Conversational RAG | Cited answers from client documents over WhatsApp, Telegram, web or email |
| ML Models | Trained models exposed to agents as tools |
| MVP, Consulting, VC | Client-facing agents (for example a Dev cell on the client's own backlog); Di-Factory's internal delivery agents run on its company platform |
| OPC Startup | An AI C-suite roster, when the client chooses this runtime |

Packs follow an **open-core** model: the core and reference packs are MIT;
production packs are Di-Factory assets; each client fully owns its instance.

## A solution spec (draft v1)

A client **instance** extends a reusable **pack** and supplies only its own
values, secrets and deployment target:

```json
{
  "spec_version": "1",
  "kind": "instance",
  "solution": { "id": "clinica-sonrisa-appointments", "version": "1.0.0", "lob": "pyme", "locale": "es-MX" },
  "extends": ["pyme-appointment-agent@^1.0"],
  "tenant": { "id": "clinica-sonrisa", "name": "Clínica Sonrisa", "timezone": "America/Mexico_City" },
  "values": {
    "business_name": "Clínica Sonrisa",
    "calendar_ids": ["dra-lopez@example.com"],
    "reminder_hours": 24
  },
  "governance": { "retention": { "conversations": "90d" } },
  "deploy": { "target": "aws", "region": "mx-central-1", "secrets_backend": "aws-secrets-manager" }
}
```

The full format and six worked examples (Appointment Agent, Service Desk
cell, Conversational RAG, Receipt Processing, Dev cell, OPC C-suite) are in [`docs/spec/`](docs/spec/SOLUTION_SPEC.md).

## Roadmap

| Milestone | Scope |
|---|---|
| **M0 Core** | Data model, event stream, agent loop, solution-spec schema, tenant ids, offline tests |
| **M1 Agent core** | Anthropic + OpenAI-compatible providers, tool registry, MCP/HTTP tools, permissions, budgets, TUI console, constructor v1 |
| **M2 Runtime** | Headless service, channels, triggers, durable queue, approvals inbox, PII/consent/audit, Postgres, instance agent, basic AWS deploy, constructor v2 |
| **M3 Intelligence** | 5-layer memory, knowledge/RAG, verification, agent teams, feedback loop, pack evals |
| **M4 Operations** | Terraform (AWS), OpenTelemetry, cost reports, region policy, fleet control plane, constructor v3 → **v1.0** |

No time goals: each milestone is done when its acceptance gate passes, and ships as a PyPI pre-release.

## Getting started (planned)

```bash
uv tool install di-factory-general-harness       # after the first release
dif-general-harness build --pack pyme-appointment-agent   # constructor: interview → spec → evals
dif-general-harness console my-solution          # run locally with a fake or real model
dif-general-harness deploy my-solution --target aws
```

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Technical design: framing, modules, all subsystems, stack, layout, roadmap, decisions |
| [`docs/PRD.md`](docs/PRD.md) | Product requirements: goals, personas, user stories, requirements, metrics, milestones, risks |
| [`docs/spec/SOLUTION_SPEC.md`](docs/spec/SOLUTION_SPEC.md) | Solution spec v1 draft: format, merge rules, validation, and paper tests on 6 example packs ([`docs/spec/examples/`](docs/spec/examples/)) |

## Tech stack

Python 3.12+ · uv · Pydantic v2 · FastAPI · Textual · PostgreSQL + pgvector ·
SQLite · official `mcp` SDK · Anthropic and OpenAI-compatible SDKs · Docker ·
Terraform · pytest · ruff · mypy

## License

[MIT](LICENSE) © 2026 Data Intelligence Factory
