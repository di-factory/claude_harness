# dif-general-harness — Architecture

Status: **Accepted: v2, general solution template** · 2026-09-27

## 0. Product framing

**dif-general-harness is Di-Factory's general solution template: an open-source
agent runtime plus pluggable capability modules. Each client solution a line of
business sells is delivered as an *instance* of that template.**

- Di-Factory (∂i~ƒ, Data Intelligence Factory, CDMX) sells seven lines of
  business (LOBs): Tailored ML Models, MVP Development, Business Consulting,
  VC Partnership, Agentic Transformation (Dev / Ops / Service Desk cells), OPC
  Startup and SMB/PyME Solutions.
- Di-Factory itself runs on OpenClaw with Paperclip. The harness is **not** the
  company's operating system. It is **one delivery option** for the solutions
  Di-Factory builds and deploys for clients.
- The PyME offer's "80% is already built, the 20% is yours" becomes concrete:
  the **80% is the template** (core and modules), and the **20% is the
  instance** (a solution spec with client prompts, rules, connectors and
  data).
- Commercial principles the template must support structurally:
  - it runs in the client's cloud and the client owns everything;
  - there is no lock-in to one model, cloud or runtime (open source, MIT);
  - third-party costs are passed through at zero markup, so costs are
    accounted per tenant and per vendor.

### 0.1 Template → instances

```
                 ┌──────────────── dif-general-harness (template, 80%) ────────────────┐
                 │ core runtime · capability modules · governance · ops · console      │
                 └───────┬───────────┬───────────┬───────────┬───────────┬─────────────┘
     solution spec (20%) │           │           │           │           │
                 PyME agents   Agentic cells  RAG assistant  ML-as-tools  Delivery agents  (OPC on
                 (6 types)     Dev/Ops/SD                    (predict)    MVP/Consult/VC   request)
```

### 0.2 Capability modules by line of business

● required · ○ optional · – not needed

| Module | PyME agents | Agentic cells | RAG assistant | ML models | Delivery agents (MVP, Consulting, VC) | OPC (if client picks harness) |
|---|---|---|---|---|---|---|
| Core loop, providers, routing, budgets | ● | ● | ● | ● | ● | ● |
| Solution spec | ● | ● | ● | ● | ● | ● |
| Channels (WhatsApp, Telegram, web, email, Slack, voice) | ● | ● | ● | ○ | – | ● |
| Triggers (schedule, webhook, event, batch) | ● | ● | ○ | ○ | ○ | ● |
| Tool registry + MCP connectors | ● | ● | ● | ● | ● | ● |
| Coding tool pack | – | ● (Dev) | – | – | ● (MVP) | ○ |
| Knowledge / RAG | ○ | ● | ● | – | ● | ○ |
| 5-layer memory | ● | ● | ○ | ○ | ● | ● |
| Agent teams (roles, handoffs, sub-agents) | ○ | ● | – | – | ○ | ● |
| Durable workflows (plans, queue, retries) | ● | ● | ○ | ○ | ○ | ● |
| Human-in-the-loop (approvals inbox, escalation) | ● | ● | ● | ○ | ● | ● |
| Verification | ● | ● | ● | ● | ● | ● |
| Governance (PII, consent, audit, retention, region) | ● | ● | ● | ● | ○ | ○ |
| ML models as tools | ○ | ○ | – | ● | – | ○ |
| Evals per solution | ● | ● | ● | ● | ● | ● |
| Tenancy, config versioning, secrets, deploy | ● | ● | ● | ● | ○ | ● |

### 0.3 Boundaries: what the harness is not

- Not Di-Factory's company OS; OpenClaw and Paperclip keep that role.
- Not an ML training platform. The ML Loop architecture stays separate, and
  trained models plug in as tools.
- Not a web-app framework. MVPs stay on the Django standard stack.
- Not a no-code builder in v1. Solution specs are files written by developers.

## 1. What we mean by "harness"

An *agent harness* is everything around the model that turns a stateless
completion API into an agent that does work: the loop, tools, context
management, persistence, permissions, hooks and observability. The model
reasons; the harness executes, remembers and constrains.

The production harnesses studied (Claude Code / Claude Agent SDK, OpenAI Agents
SDK, Microsoft Agent Framework, Pydantic AI, LangGraph, and the open-source
coding agents surveyed in recent literature) converge on the same components:
the agent loop, provider adapters, tool registry (incl. MCP), context
management, session persistence, prompt assembly, hooks, permissions and
human-in-the-loop, sub-agents, observability and evals.

## 2. Options considered

| | A. Build on a framework (LangGraph / Pydantic AI / MAF) | B. Wrap the Claude Agent SDK | C. Own core loop, thin adapters (**chosen**) |
|---|---|---|---|
| Time to first demo | Fast | Fastest | Medium |
| Control over loop & context | Limited by framework abstractions | Low | Full |
| Provider portability | Good | Claude only | Good, via adapter protocol |
| Debuggability | Framework stack traces, graph DSL | Opaque subprocess | Plain Python |
| Dependency / churn risk | High | Medium | Low |
| Fit with "no lock-in" promise | Framework lock-in | Vendor lock-in | Client owns plain Python |

**Why C.** Anthropic's engineering guidance favours a hand-rolled loop, a few
high-signal tools, just-in-time retrieval, orchestrator–worker sub-agents and
explicit context budgeting. The loop is small; the value is in context, tools,
safety and governance, which are the parts frameworks hide. Components encode
assumptions about what models can't do; those expire, so they must stay cheap to
change. We borrow best-in-class libraries (Pydantic, provider SDKs, `mcp`).

## 3. Architecture

```
  Surfaces      Channels + triggers (webhooks, schedules, chat gateways)
                Admin / approvals API (FastAPI)  ·  TUI operator console (Textual)  ·  Python API
                                         │ events (async stream)
  Instance      Solution spec loader → tenant-scoped Instance (agents, tools, policies, channels…)
                                         │
  Core          Agent loop ── PromptBuilder · ContextManager · Hooks · PermissionPolicy
                            · Guardrails & budgets · Verifier · Router · Workflow engine (plans, queue)
                                         │
  Modules       Providers · Tool registry + MCP · Tool packs · Knowledge (RAG) · Memory (5 layers)
                · Agent teams · HITL inbox · ML-model tools · Evals
                                         │
  Governance    PII tokenization · consent · immutable audit log · retention · provider-region policy
                                         │
  Platform      Storage (SQLite/JSONL local · Postgres+pgvector prod) · Secrets vault adapters
                · Executor (subprocess → container) · Checkpoints · Telemetry & cost · Deploy (Docker, Terraform)
```

### 3.0 Solution spec (the 20%)

A solution is a versioned, declarative spec (JSON, validated by Pydantic), with
prompt files and optional Python extensions next to it:

| Section | Declares |
|---|---|
| `solution` | id, version, LOB, description, locale (es-MX first) |
| `agents` | named agents: role, system prompt file, model role, tools, memory policy, sub-agents |
| `models` | per-role `{provider, model, effort}` (§3.11) and allowed provider regions |
| `tools` | tool packs, MCP servers, HTTP/REST connectors (JSON-schema contracts), ML-model tools |
| `knowledge` | corpora to ingest and retrieval settings (§3.19) |
| `channels` / `triggers` | inbound channels and schedules/webhooks/events (§3.15) |
| `workflows` | durable multi-step flows and handoffs between agents (§3.16) |
| `policies` | permissions, guardrail profile, budgets, verification, escalation (§3.6, §3.10, §3.12) |
| `governance` | PII classes, consent rules, retention, audit level (§3.18) |
| `evals` | eval set and pass thresholds for this solution (§3.21) |
| `deploy` | deployment profile (local, container, AWS Terraform) and required secrets |

A **pack** is a reusable, versioned spec fragment (for example a
"PyME Appointment Agent" base, or a "Service Desk cell" base). An **instance**
= pack(s) + client overrides + tenant id. Onboarding a client means writing an
override file and connecting secrets; it does not mean new code.

### 3.1 Core data model (provider-neutral)

- `Message(role, content: list[Block])`, where `Block` is `Text | ToolUse |
  ToolResult | Thinking | Image | Document`.
- `Event`: everything the loop emits (`TextDelta`, `ToolCallStarted`,
  `ToolCallFinished`, `ApprovalRequested`, `Escalated`, `Compacted`,
  `TurnEnded`, `Error`). Channels, console, logs and stores all consume it.
- `Session`: id, **tenant id, instance id, agent id, contact/conversation
  key**, config version and message history. It is reconstructible from its
  event log, which makes resume and fork straightforward.

### 3.2 Agent loop

```python
async def run(session, user_input) -> AsyncIterator[Event]:
    session.append(user(user_input))
    while True:
        ctx = context_manager.prepare(session)          # budget / compact / pin
        async for ev in provider.stream(ctx, tools.schemas()):
            yield ev
        msg = provider.final_message()
        session.append(msg)
        calls = msg.tool_uses()
        if not calls or stop_conditions.hit(session):
            yield TurnEnded(...); return
        results = await tools.execute_all(calls, policy, hooks)  # parallel
        session.append(tool_results(results))
```

The loop is async-first. The same loop runs interactively (console) and
headless (a channel message, schedule or workflow step starts a turn).

### 3.3 Model providers

A small `Protocol` (`stream`, `count_tokens`, `capabilities`). The core only sees
the neutral model; provider features (prompt caching, thinking, server tools)
are exposed via `capabilities` and degrade gracefully. **Model-agnostic from
the first usable release:** `AnthropicProvider` (direct API or Bedrock/Vertex)
and an `OpenAICompatibleProvider` (OpenAI, vLLM, Ollama, gateways), plus
`FakeProvider` for tests. A shared conformance suite keeps them
interchangeable. Swapping a model is a spec change, validated by the solution's
evals.

### 3.4 Tools

- **Tools are contracts.** Every tool is a JSON-schema entry in a registry,
  sourced from Python `@tool` functions, MCP servers (stdio and streamable
  HTTP), or declarative HTTP/REST connectors. Adding a capability is a
  registry entry, not an agent redeploy.
- Each tool declares `effect: read | write | external` and a permission
  category. Calls with side effects (`write`, `external`) go through
  verification and approval policy.
- The model never executes anything itself: the harness validates, checks
  policy, executes and **always** returns a structured observation
  (`ok | denied | error | timeout`, plus payload).
- **Tool packs** bundle tools, prompt fragments and default permissions:
  - `coding`: read/write/edit files, bash, grep, glob;
  - `general`: web fetch/search, HTTP, notes;
  - connector packs (calendar, CRM, messaging, spreadsheets, ERP), added
    as instances need them;
  - `ml`: call a deployed model endpoint for predict/score.
- **Checkpoints:** file writes are snapshotted for `/undo` and rewind. For
  external actions, the audit log records what was done, so compensating
  actions are possible.
- Sub-agent = a tool that runs a child session with a restricted tool set,
  returning only its final answer.

### 3.5 Context management

- Token budget per request; prompt-cache breakpoints on the stable prefix.
- Compaction with an overflow handler: decisions and facts are saved to
  memory before older turns are summarised.
- Just-in-time retrieval; large tool results are stored and referenced.
- **Pinned block:** the instance's system rules, approved constraints and
  user-stated task constraints are never summarised away.
- Cost short-circuit: an optional intent router answers trivial requests
  (FAQ, hours, greetings) without calling the main model.

### 3.6 Permissions and approvals

- `PermissionPolicy`: allow / ask / deny rules per tool, argument pattern
  and effect. Rules are declared in the spec and can be tightened by the
  tenant.
- `ask` emits `ApprovalRequested`. **Interactive:** the console prompts.
  **Headless:** the request goes to the approvals inbox (§3.17), and the
  step waits durably until a person approves, rejects or it times out.
- Execution goes through a pluggable `Executor`: `SubprocessExecutor` first,
  with `ContainerExecutor` later.

### 3.7 Hooks

Lifecycle points: `session_start`, `pre_tool`, `post_tool`, `pre_compact`,
`stop`, `on_message_in`, `on_message_out`, `on_escalate`. Hooks are Python
callables or shell commands; they can veto, modify or inject context.

### 3.8 Observability & cost

- Every event is logged per tenant, instance and session, so any request can
  be replayed and explained ("why did this cost 40 cents and take 8 seconds").
- **Cost is accounted per tenant, per vendor and per role.** This backs the
  zero-markup cost-transparency clause.
- Quality metrics: tool success rate, verify pass rate, escalation rate, cost
  per resolved request.
- OpenTelemetry export (GenAI semantic conventions) is opt-in, for Langfuse,
  Phoenix or Grafana.

### 3.9 Memory subsystem (5 layers)

Adopted from *Agent Memory — The 5-Layer Playbook* (CoALA-based). Memory sits
behind a `MemoryBackend` protocol. It is **scoped per tenant and instance**,
and per contact where relevant (for example a clinic patient), so nothing
leaks between clients or between contacts.

| Layer | Holds | Component | Storage (local · prod) | Expiry |
|---|---|---|---|---|
| 1. Working | Current context | `ContextManager` | context window | end of call |
| 2. Episodic | What happened | `EpisodicStore` | JSONL · Postgres | TTL per governance policy, pins |
| 3. Semantic | What is true | `SemanticStore` + ontology | SQLite FTS5 · Postgres (+pgvector later) | on supersession |
| 4. Procedural | How to do things | skills, versioned | files · Postgres | on version update |
| 5. Forgetting | What to delete | `ForgettingEngine` | — | scheduled |

The data flow runs from working overflow into episodic, distils into semantic
and encodes into procedural, with retrieval at session start and through
`memory_search` / `memory_write`. Guardrails:
- the model always drives; skills are loaded into context, never executed on
  their own;
- skill promotion needs human approval;
- contradictions are flagged to a person;
- the memory block has a token budget;
- retention obeys the governance policy.

### 3.10 Verification (maker ≠ checker)

1. **Deterministic checks:** configured commands, schema/business-rule
   validators, and scope checks.
2. **Verifier agent** (opt-in per spec): separate prompt and model role, and
   read-only tools.

**Every state-changing external action passes verification before it
commits** when the spec marks it critical (for example no duplicate bookings
and no duplicate invoices). If verification fails, the reason goes back to the
maker, with retries capped; the step can also escalate to a person.

### 3.11 Model routing

Per-role config `{provider, model, effort}`: `main`, `subagent`, `verifier`,
`compaction`, `memory_extraction`, `router` (intent short-circuit), `title`.
It is set in the spec and can be overridden per agent. There is no automatic
classifier in v1.

### 3.12 Guardrails

| Category | Mechanism |
|---|---|
| Tool & action | permission policy, approvals, verification of side effects, checkpoints |
| Scope | path/resource allowlists per agent |
| Operational | budgets on tokens, USD, turns, retries and wall time per run and per tenant per day, on by default. Interactive runs pause and ask; headless runs stop and notify the approvals inbox. |
| Data | secret redaction in logs, traces and memory; secret-file deny list; **PII tokenization** (§3.18) |
| Behavioral | content/output policy, restricted topics (medical, legal, financial advice) that trigger escalation, as optional modules per spec |

Profiles (`strict`, `default`, `fast`) set how much friction the solution gets.

### 3.13 Feedback loop

Verifier rejections, denials, budget overruns, escalation outcomes and 👍/👎
from channels produce **candidate constraints**. They go to the approvals
inbox, and approved rules join the instance's pinned constraints. Every
learned instruction is approved by a person.

### 3.14 Tenancy and configuration

- **Tenant** is a first-class scope on every record: sessions, memory,
  knowledge, tools, config, traces, costs and retention.
- The default delivery is **single-tenant: a dedicated deployment in the
  client's cloud**. Because the code always namespaces by tenant, a
  multi-tenant deployment is a configuration choice, not a rewrite.
- Instance configuration is **versioned and hot-reloadable, with rollback**.
  A model swap, a policy change or a new tool is a config change, never a
  redeploy.
- **Secrets** are referenced by name and resolved through vault adapters
  (env/file locally; AWS Secrets Manager, GCP Secret Manager or 1Password in
  production). Di-Factory never needs to see the values.

### 3.15 Channels and triggers

- **Channel adapters** normalize inbound messages into a canonical envelope
  (tenant, channel, contact, message, attachments), and send outbound
  messages and templates:
  - chat through a messaging gateway (Twilio-style) for WhatsApp and SMS;
  - Telegram, web widget and email;
  - Slack;
  - REST API;
  - voice later (speech-to-text / text-to-speech).
- **Triggers** start work without a user message: cron schedules, delayed
  jobs ("remind 24 h before"), webhooks from client systems, and batch runs.
- Each contact gets a conversation key, so sessions and memory follow the
  person across messages.

### 3.16 Durable workflows and agent teams

- **Workflow engine:** plans and multi-step flows are persisted as
  inspectable, replayable artifacts. Steps run from a durable queue
  (Postgres-native by default) with retries, timeouts and waiting for
  approvals or external events.
- **Agent teams:** named, long-lived agents with roles (for example the Dev,
  Ops and Service Desk cells, or a CGO/COO/CTO roster). They coordinate
  through handoffs and a shared task ledger, and each can own sub-agents. The
  orchestrator + sub-agent pattern stays the simplest case.

### 3.17 Human-in-the-loop inbox and escalation

- One **inbox** for approval requests, escalations, constraint/skill proposals
  and budget stops. It is exposed through the admin API, the console, and
  notifications on a chosen channel (for example Telegram or email).
- **Escalation is a first-class branch, not an error.** It hands the
  conversation, plan, tool trace and verification verdict to a person or to
  the client's helpdesk (Zendesk, Freshdesk or ServiceNow through
  connectors).

### 3.18 Governance plane (Mexican regulations first)

- **PII tokenization:** names, phones, emails, CURP, RFC and account numbers
  are replaced with reversible tokens before content reaches the model.
  Tokens are resolved only at output time, where the policy allows.
- **Consent and opt-out** are tracked per contact and channel, and are
  honoured by triggers.
- An **immutable audit log** records who and what acted, when, and on which
  data, across all stages.
- **Retention** policies apply per data class and tenant, enforced by the
  forgetting engine.
- **Provider-region policy:** a tenant can forbid routing to models outside
  allowed regions.
- Compliance targets are LFPDPPP first; CNBV and NOM-024 profiles come later.

### 3.19 Knowledge module (RAG)

- Ingest documents (PDF, DOCX, XLSX, HTML, text, images with OCR) and
  connectors (Drive, SharePoint, Notion, S3), with provenance, versioning and
  deletion that propagates to the index.
- Layout-aware chunking; hybrid retrieval (BM25 plus embeddings); cited
  answers; and "not found" when no chunk scores above the threshold.
- It is exposed to agents as tools (`search_knowledge`), so RAG is a module,
  not a separate product.

### 3.20 Storage, deployment and operations

| Concern | Local / console | Production |
|---|---|---|
| Sessions, events | JSONL | Postgres |
| Memory, knowledge | SQLite + FTS5 | Postgres + FTS, pgvector |
| Queue | in-process | Postgres-native queue (Temporal/Celery only if needed) |
| Secrets | env / file | client vault adapter |
| Deploy | `dif-general-harness` CLI | Docker image; Terraform module (AWS first: ECS Fargate or EC2, RDS, Secrets Manager); GCP/Azure later |

Operations: health checks; config rollback; per-instance dashboards via the
admin API; and a runbook generated from the spec.

### 3.21 Evals per solution

Every pack ships an eval set: scripted conversations and tasks with expected
outcomes and tool calls. Evals run offline against `FakeProvider` and against
real models before any model swap, pack upgrade or config release. Results are
stored so behavioural drift is visible over time.

## 4. Tech stack

| Concern | Choice |
|---|---|
| Python | 3.12+ · MIT · PyPI `di-factory-general-harness` |
| Packaging | `uv`, `pyproject.toml` |
| Schemas / config / specs | Pydantic v2; JSON specs and settings |
| LLM transport | `anthropic` SDK, `openai` SDK (OpenAI-compatible endpoints) |
| MCP | official `mcp` Python SDK |
| Service / API / webhooks | FastAPI + Uvicorn |
| Storage | SQLite (local), PostgreSQL + pgvector (prod) |
| Console | Textual (TUI), launched by a Typer CLI |
| Deploy | Docker; Terraform (AWS first) |
| Tests | pytest + pytest-asyncio, FakeProvider, conformance and eval suites |
| Lint / types | ruff, mypy (strict on `core/`) |

## 5. Layout

```
src/dif_general_harness/
  core/        loop.py events.py messages.py session.py context.py
  spec/        schema.py loader.py packs.py instance.py
  providers/   base.py anthropic.py openai_compat.py fake.py
  tools/       registry.py mcp.py http_connector.py subagent.py packs/(coding, general, connectors, ml)
  policy/      permissions.py hooks.py guardrails.py budgets.py redact.py
  verify/      checks.py verifier.py
  memory/      base.py episodic.py semantic.py procedural.py forgetting.py ontology.py
  knowledge/   ingest.py chunk.py retrieve.py
  workflows/   engine.py queue.py teams.py
  channels/    base.py gateway.py telegram.py web.py email.py
  triggers/    scheduler.py webhooks.py
  hitl/        inbox.py escalation.py
  governance/  pii.py consent.py audit.py retention.py region.py
  tenancy/     tenant.py config_versions.py secrets.py
  store/       jsonl.py sqlite.py postgres.py
  service/     app.py (FastAPI: channels, webhooks, admin, inbox)
  console/     tui/ (Textual)  cli.py
  routing.py   checkpoints.py  telemetry.py
packs/         reusable solution packs (spec fragments + prompts + evals)
deploy/        docker/  terraform/aws/
tests/  evals/  docs/
```

## 6. Roadmap

1. **M0 – Core skeleton:**
   - data model and events;
   - loop;
   - FakeProvider;
   - JSONL store;
   - **solution-spec schema and loader**;
   - tenant/instance ids on every record;
   - offline tests.
2. **M1 – Agent core:**
   - Anthropic and OpenAI-compatible providers, with a conformance suite;
   - tool registry with structured observations;
   - MCP client and HTTP connectors;
   - coding and general packs;
   - permissions and approvals model;
   - budgets;
   - secret redaction;
   - routing;
   - checkpoints;
   - **TUI console** for building and testing instances locally.
3. **M2 – Headless runtime and governance minimum:**
   - FastAPI service;
   - channel adapters (a messaging gateway first) and triggers (scheduler,
     webhooks);
   - durable queue;
   - Postgres backend;
   - approvals inbox and escalation;
   - secrets vault adapters;
   - versioned config;
   - Docker image;
   - PII tokenization, consent/opt-out, audit log and retention.
4. **M3 – Intelligence modules:**
   - 5-layer memory;
   - knowledge/RAG;
   - verification (both tiers);
   - feedback loop;
   - agent teams and workflows;
   - evals per solution.
5. **M4 – Operations hardening:**
   - Terraform (AWS);
   - OpenTelemetry;
   - cost reports per tenant and vendor;
   - provider-region policy;
   - ContainerExecutor;
   - GCP/Azure profiles.

**Instances** follow the template. The first candidates are listed in §8; an
instance can start once the modules it needs have shipped.

## 7. Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Product scope | **General solution template** for Di-Factory LOB solutions: core + capability modules + solution specs; instances are built on it. §0 |
| 2 | Relation to OpenClaw/Paperclip | Separate. OpenClaw/Paperclip run Di-Factory; the harness is one delivery option for client solutions. |
| 3 | Primary surface | **Headless runtime first** (channels, triggers, admin API); TUI is the developer/operator console. |
| 4 | Providers | Provider-neutral core; **Anthropic + OpenAI-compatible** from M1, conformance-tested. |
| 5 | Tenancy | **Single-tenant per client cloud by default**, multi-tenant capable (tenant id on every record). |
| 6 | Governance | **Minimum set before the first client instance**: PII tokenization, consent/opt-out, audit log, retention. Region policy and CNBV/NOM profiles later. |
| 7 | Deployment | **AWS first**: Docker image + Terraform; GCP/Azure later. Local mode for development. |
| 8 | Sandboxing | Pluggable Executor; subprocess first, containers later. |
| 9 | Memory | 5 layers, scoped per tenant / instance / contact; SQLite FTS5 locally, Postgres in production. |
| 10 | Multi-agent | Orchestrator + sub-agents **and** agent teams with roles, handoffs and a shared ledger (M3). |
| 11 | Config format | JSON specs and settings validated by Pydantic; versioned, hot-reloadable. |
| 12 | Secrets | Referenced by name, resolved via vault adapters; env/file locally, never in specs. |
| 13 | Default permissions | Reads allowed; writes, shell, network and external side effects ask; headless asks go to the approvals inbox. |
| 14 | Verification | Deterministic checks + optional verifier agent; critical side effects verified before commit. |
| 15 | Routing | Per-role model config incl. `router` for intent short-circuit; no auto-classifier in v1. |
| 16 | Feedback | Candidate constraints approved by a person via the inbox; pinned. |
| 17 | Budgets | On by default, per run and per tenant per day; interactive pauses, headless stops and notifies. |
| 18 | Checkpoints | Snapshots for file writes; audit trail for external actions. |
| 19 | Data guardrails | Secret redaction + deny list + PII tokenization. |
| 20 | Observability | Per-tenant event log and cost by vendor; OpenTelemetry opt-in. |
| 21 | Evals | Every pack ships an eval set; run before model swaps and releases. |
| 22 | Packaging | Python 3.12+, MIT, PyPI `di-factory-general-harness`, import `dif_general_harness`, CLI `dif-general-harness`. |

## 8. First instantiation candidates (parked)

These choices were made for the first instance and stay parked until the
template modules they need exist:

| Choice | Value |
|---|---|
| First pack | PyME **Appointment Agent** (clinics: confirm, remind, reschedule) |
| Channel | WhatsApp/SMS through a **messaging gateway** (Twilio-style) |
| Calendar | **Google Calendar** connector |
| Deployment | AWS, container + Terraform, single-tenant |
| Governance | Minimum set (PII tokenization, consent, audit, retention) |

## References

- Di-Factory website (di-factory.biz): lines of business, reference
  architectures (Agentic, Conversational RAG, OPC, ML Loop, MVP), About.
- Anthropic engineering: Building Effective Agents; Effective harnesses for
  long-running agents; Demystifying evals for AI agents.
- Microsoft Agent Framework at BUILD 2026.
- Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents
  (arXiv 2609.00006).
- Agent Memory — The 5-Layer Playbook (independent compilation; metrics
  unverified).
- "Harness Engineering: the skill that replaced prompt engineering in 2026"
  (X post; statistics unsourced).
- Sumers et al., Cognitive Architectures for Language Agents (CoALA).
