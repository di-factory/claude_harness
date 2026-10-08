# dif-general-harness

**The general solution template behind Di-Factory's agentic solutions:** one of
Di-Factory's base platforms, used to run, adjust and deploy client solutions
across its lines of business.
One open-source agent runtime, many client solutions: each one is a
declarative *solution spec* on top of a shared core.

> **Status: v1.0 (M0–M4 done) plus decisions 58–92.**
>
> - **Channels:** WhatsApp/SMS, Telegram, a web chat with its landing page, REST (and an
>   OpenAI-compatible `/v1` endpoint), email, Slack and voice.
> - **Triggers:** schedule, webhook, event, delay, relative, file and batch.
> - **Building blocks:**
>   - durable workflows and agent teams;
>   - scoped memory;
>   - knowledge with citations and a list of what it could not answer;
>   - skills;
>   - research graphs.
> - **Safety and recovery:**
>   - verification before side effects;
>   - side-effecting calls never repeated blindly;
>   - untrusted text fenced.
> - **Built-in improvement loop:**
>   - real conversations replayed before every rebuild goes online, and nightly;
>   - run records and a weekly review that proposes edits a person approves;
>   - evals with pass^k and ablations.
> - **Operations:**
>   - governance: PII, consent, audit, provider regions;
>   - cost reports by tenant and vendor;
>   - opt-in OpenTelemetry;
>   - a sandboxed shell with an egress proxy;
>   - a hardened AWS module;
>   - a control plane for eval-gated fleet rollouts;
>   - a guided handover to the client's own Claude.
>
> Deploys need Jag's signed approval.

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
  → constructor matches S to the pack catalog (no fit → new pack needed)
  → interviews for the open details the pack declares (./setup.sh guides it on a server;
    a client's web site is read and written up as its FAQ)
  → writes the instance spec (the 15–20%), validates it, runs the pack's evals
  → every rebuild replays the latest real conversations before it goes online
  → Jag approves (signs exactly what ships)
  → deploys into X's own cloud; X keeps its secrets in its own vault
  → /handover leaves it with X's own Claude; the instance agent reports to the control plane
```

Once it runs, it watches itself: a nightly replay, run records and a weekly review that
proposes edits (never applies them), all landing in the inbox (`admin inbox`, `admin review`).

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
Surfaces     channels & triggers · admin API + CLI · OpenAI-compatible /v1 · signed hooks · TUI
Instance     solution spec  →  tenant-scoped instance
Core         agent loop with caps · context (skills, rules, fenced memory, compaction)
             permissions · intent log · verifier · router · workflow engine (foreach, gates)
Modules      providers · tools + MCP · knowledge (RAG) · memory · agent teams
             research graphs · documents/OCR · HITL inbox · evals
Improvement  run records · weekly review · replay · pass^k and ablations · feedback rules
Governance   PII tokens · consent · audit log · retention · region policy · untrusted fences
Platform     storage (SQLite / Postgres) · secrets · sandbox + egress proxy · deploy · control plane
```

We own the agent loop instead of wrapping a framework, so context handling,
safety and cost stay visible and cheap to change. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design and all 93
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

The full format and seven worked examples (Appointment Agent, Service Desk cell,
Conversational RAG, Receipt Processing, Dev cell, OPC C-suite, Research Graph) are in
[`docs/spec/`](docs/spec/SOLUTION_SPEC.md).

## Roadmap

| Milestone | Scope |
|---|---|
| **M0 Core** ✓ | Data model, event stream, agent loop, solution-spec schema, tenant ids, offline tests |
| **M1 Agent core** ✓ | Anthropic + OpenAI-compatible providers, tool registry, MCP/HTTP tools, permissions, budgets, TUI console, constructor v1 |
| **M2 Runtime** ✓ | Headless service, channels, triggers, durable queue, approvals inbox, PII/consent/audit, Postgres, instance agent, basic AWS deploy, constructor v2 |
| **M3 Intelligence** ✓ | 5-layer memory, knowledge/RAG, verification, agent teams, feedback loop, pack evals |
| **M4 Operations** ✓ | Terraform (AWS), OpenTelemetry, cost reports, region policy, fleet control plane, constructor v3 → **v1.0** |
| **After v1.0** ✓ | Decisions 58–92: compaction, file/batch triggers, documents, voice, web chat, guided setup, handover; replay and the nightly watch; skills, hooks, `/v1`; run records, gates, weekly review, research graphs; recovery (intent log, actionable failures), pass^k and ablations |
| **Next** | GCP and Azure profiles; streaming voice, more knowledge connectors, pgvector, a first real AWS apply |

No time goals: each milestone is done when its acceptance gate passes, and ships as a tagged pre-release in this repository (not on PyPI).

## Getting started

**New here?** On a fresh Ubuntu server: `git clone … && cd claude_harness && ./setup.sh`. It
installs everything, runs the client questionnaire, tests the agent and can put it online over
HTTPS. [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) explains each step.

Build and try a solution locally:

```bash
uv sync
uv run pytest -q                                   # offline: no API keys needed

# specs
uv run dif-general-harness spec validate docs/spec/examples/pyme-appointment-agent
uv run dif-general-harness spec resolve docs/spec/examples/instances/clinica-sonrisa.json

# onboarding: a questionnaire per side; the client's business answers become its FAQ
uv run dif-general-harness questionnaire --pack pyme-appointment-agent --packs docs/spec/examples \
    --for client --out client.yaml
uv run dif-general-harness secrets set anthropic        # stored in ~/.dif/secrets

# constructor v1: match a pack, interview, write + validate the instance spec
uv run dif-general-harness build --packs docs/spec/examples --out instances \
    --request "appointment reminders for a dental clinic on WhatsApp"
#   (add --answers answers.yaml for a non-interactive build; outputs <id>.json,
#    <id>.answers.yaml and <id>.summary.md for approval)

# a new client from an example (copies the files the instance references too)
uv run dif-general-harness spec copy docs/spec/examples/instances/clinica-sonrisa.json clients/demo

# run an instance locally (secrets: DIF_SECRET_<NAME> env vars or --secrets-dir)
export DIF_SECRET_ANTHROPIC=...
uv run dif-general-harness console instances/<id>.json --packs docs/spec/examples
uv run dif-general-harness run instances/<id>.json --packs docs/spec/examples -m "Hola"
uv run dif-general-harness eval instances/<id>.json --packs docs/spec/examples \
    [--repeat 3] [--ablate skills,verifier]       # pass^k; does each component still pay?
```

Run it as a service:

```bash
# channels, triggers, inbox and admin API; Postgres in production
export DIF_SECRET_ADMIN_TOKEN=... DIF_DATABASE_URL=postgresql://...
uv run dif-general-harness serve instances/<id>.json --packs docs/spec/examples \
    --public-url https://<the-url-providers-call>

# deploy (constructor v2): Jag signs exactly what ships
uv run dif-general-harness keys new jag                  # once; add the public key to .dif/approvers.json
uv run dif-general-harness approve instances/<id>.json --packs docs/spec/examples \
    --target aws --key ~/.dif/keys/jag.key --by jag
uv run dif-general-harness deploy instances/<id>.json --packs docs/spec/examples --target aws
#   docker: writes docker-compose.yml (instance + Postgres); aws: terraform.tfvars.json for
#   deploy/terraform/aws plus the exact commands (--run executes them)
```

Operate a running client and change it safely:

```bash
uv run dif-general-harness admin status | inbox | show ID | reply ID TEXT
uv run dif-general-harness admin faq show | set FILE | gaps        # an edit is replayed first
uv run dif-general-harness admin runs | review [--now] | graph NAME [QUERY]
uv run dif-general-harness replay clients/<id>.json --packs docs/spec/examples  # real conversations, again
uv run dif-general-harness adjust|upgrade clients/<id>.json ... --dry-run
uv run dif-general-harness handover clients/<id>.json --owner NAME --lang es      # or /handover
```

Install a tagged release (no PyPI):

```bash
uv tool install git+https://github.com/di-factory/claude_harness@v1.0.0
```

## Documentation

| Document | Contents |
|---|---|
| [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) | Step by step from a clean server to a live agent, and the common setup mistakes |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Technical design: framing, modules, all subsystems, stack, layout, roadmap, decisions |
| [`docs/PRD.md`](docs/PRD.md) | Product requirements: goals, personas, user stories, requirements, metrics, milestones, risks |
| [`docs/spec/SOLUTION_SPEC.md`](docs/spec/SOLUTION_SPEC.md) | Solution spec v1: format, merge rules, validation, and seven example packs ([`docs/spec/examples/`](docs/spec/examples/)) |
| [`CLAUDE.md`](CLAUDE.md) | For agents working on this repository: commands, layout, rules that must not be broken |
| [`integrations/openclaw-skill/SKILL.md`](integrations/openclaw-skill/SKILL.md) | The skill Teky uses to drive the constructor: build, verify, replay, ask Jag to approve, deploy, adjust, upgrade |
| [`.claude/skills/handover/SKILL.md`](.claude/skills/handover/SKILL.md) | `/handover`: hand a finished solution to its client's own Claude, step by step |
| [`docs/testing/dev-cell/README.md`](docs/testing/dev-cell/README.md) | Dev Cell live test kit: a throwaway repository, three test issues ([`01`](docs/testing/dev-cell/issues/01-off-by-one.md), [`02`](docs/testing/dev-cell/issues/02-vague.md), [`03`](docs/testing/dev-cell/issues/03-injection.md)), the sample repo ([`README`](docs/testing/dev-cell/sample-repo/README.md), [`CLAUDE.md`](docs/testing/dev-cell/sample-repo/CLAUDE.md)) and what counts as a pass |

### Example packs (also the test fixtures)

Each pack in [`docs/spec/examples/`](docs/spec/examples/) is a `pack.json` plus the Markdown it
references: agent prompts, message templates, knowledge files and skills.

| Pack | Markdown files |
|---|---|
| conversational-rag | [`knowledge/about.md`](docs/spec/examples/conversational-rag/knowledge/about.md), [`prompts/assistant.md`](docs/spec/examples/conversational-rag/prompts/assistant.md) |
| dev-cell | [`prompts/developer.md`](docs/spec/examples/dev-cell/prompts/developer.md) |
| instances (clinica-sonrisa) | [`clinica-sonrisa/tpl_nudge.es-MX.md`](docs/spec/examples/instances/clinica-sonrisa/tpl_nudge.es-MX.md), [`clinica-sonrisa/tpl_reminder.es-MX.md`](docs/spec/examples/instances/clinica-sonrisa/tpl_reminder.es-MX.md) |
| opc-c-suite | [`prompts/cgo.md`](docs/spec/examples/opc-c-suite/prompts/cgo.md), [`prompts/content_writer.md`](docs/spec/examples/opc-c-suite/prompts/content_writer.md), [`prompts/coo.md`](docs/spec/examples/opc-c-suite/prompts/coo.md), [`prompts/cto.md`](docs/spec/examples/opc-c-suite/prompts/cto.md) |
| pyme-appointment-agent | [`knowledge/faq.md`](docs/spec/examples/pyme-appointment-agent/knowledge/faq.md), [`prompts/receptionist.md`](docs/spec/examples/pyme-appointment-agent/prompts/receptionist.md), [`prompts/tpl_nudge.md`](docs/spec/examples/pyme-appointment-agent/prompts/tpl_nudge.md), [`prompts/tpl_reminder.md`](docs/spec/examples/pyme-appointment-agent/prompts/tpl_reminder.md), [`skills/complaint/SKILL.md`](docs/spec/examples/pyme-appointment-agent/skills/complaint/SKILL.md) |
| pyme-receipt-processing | [`prompts/extractor.md`](docs/spec/examples/pyme-receipt-processing/prompts/extractor.md) |
| research-graph | [`prompts/analyst.md`](docs/spec/examples/research-graph/prompts/analyst.md), [`prompts/researcher.md`](docs/spec/examples/research-graph/prompts/researcher.md), [`skills/entity-research/SKILL.md`](docs/spec/examples/research-graph/skills/entity-research/SKILL.md) |
| service-desk-cell | [`prompts/kb_researcher.md`](docs/spec/examples/service-desk-cell/prompts/kb_researcher.md), [`prompts/resolver.md`](docs/spec/examples/service-desk-cell/prompts/resolver.md), [`prompts/triage.md`](docs/spec/examples/service-desk-cell/prompts/triage.md) |

## Tech stack

Python 3.12+ · uv · Pydantic v2 · FastAPI · Textual · PostgreSQL · SQLite ·
official `mcp` SDK · Anthropic and OpenAI-compatible SDKs · Docker · Terraform ·
pytest · ruff · mypy

## License

[MIT](LICENSE) © 2026 Data Intelligence Factory
