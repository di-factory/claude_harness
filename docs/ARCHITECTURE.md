# dif-general-harness — Architecture

Status: **Accepted: v2, general solution template** · last updated 2026-09-29

## 0. Product framing

**dif-general-harness is Di-Factory's general solution template: an open-source
agent runtime plus pluggable capability modules. Each client solution a line of
business sells is delivered as an *instance* of that template.**

- Di-Factory (∂i~ƒ, Data Intelligence Factory, CDMX) sells seven lines of
  business (LOBs): Tailored ML Models, MVP Development, Business Consulting,
  VC Partnership, Agentic Transformation (Dev / Ops / Service Desk cells), OPC
  Startup and SMB/PyME Solutions.
- Di-Factory itself runs on OpenClaw with Paperclip. The harness is **not** the
  company's operating system. It is **one base platform among several** (next
  to OpenClaw/Hermes, the Django MVP stack and the ML Loop), used to **run,
  adjust and deploy** client solutions across every line of business where it
  applies.
- Delivery agents that Di-Factory uses internally (business-plan research,
  due diligence, internal MVP work) stay on OpenClaw/Paperclip. When a client
  buys one of those agents (for example a Dev cell for their own backlog), it
  runs on the harness in the client's cloud.
- Solution specs are **harness-native**: clean and documented, so exporters
  to other platforms can be added later. There is no cross-platform promise in
  v1.
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

| Module | PyME agents | Agentic cells | RAG assistant | ML models | Client-facing delivery agents (MVP, Consulting, VC) | OPC (if client picks harness) |
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
- Not where Di-Factory's internal delivery agents run (they stay on
  OpenClaw/Paperclip); only client-facing ones run here.

### 0.4 How a client solution gets built: the constructor agent

The operating model: when Di-Factory sells a solution to client X, the
**constructor agent** turns the request into a running instance in X's cloud.

```
1. Intake     "deploy solution S for client X"            (from Jag, or from Teky on OpenClaw)
2. Match      S checked against the pack catalog  → fits a pack, or a combination → continue
                                                  → no fit → stop: a new pack is Di-Factory design work
3. Interview  asks only what the chosen packs declare as open (their questionnaire):
              business details, channels, integrations, language, compliance, target cloud account
4. Build      writes the instance spec (the 15–20%); any custom-code extension is flagged
5. Verify     validates the resolved spec and runs the packs' evals against it in a sandbox
6. Approve    plain-language summary + resolved spec + eval results → Jag approves
7. Deploy     into the client's cloud; the client enters secrets straight into their own vault
8. Hand over  smoke test, instance agent registers with the control plane, runbook delivered
```

- The constructor never invents missing capability. A request no pack covers
  becomes a pack-design task, keeping the 80/20 model honest.
- The questions come from the packs (`variables[*].ask`, see the spec), so a
  new pack brings its own interview without changing the constructor.
- The constructor ships **with the harness** (`dif-general-harness build`),
  defined as a harness pack itself. A thin **OpenClaw skill** lets Teky
  (Di-Factory's CTO agent) drive it.
- **Only Jag approves deployments** (decision 37). The constructor never sees
  secret values.
- Adjusting a live instance uses the same path: change the answers → rebuild
  → verify → approve → roll out as a new config version.

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

Full draft: [`spec/SOLUTION_SPEC.md`](spec/SOLUTION_SPEC.md).

A solution is a versioned, declarative spec (JSON, validated by Pydantic), with
prompt files and optional Python extensions next to it:

| Section | Declares |
|---|---|
| `solution` | id, version, LOB, description, locale (English default) |
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
  - `ml`: call a deployed model endpoint for predict/score;
  - `documents`: read, OCR and parse files (PDF, images, CFDI XML).
- **Built-in tools:** `ledger.*` (team task ledger), `runs.*` (run
  summaries), `knowledge.*` and `memory.*`. Packs declare the namespaces they
  expose.
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
- It is exposed to agents as tools (`knowledge.search_<corpus>`), so RAG is a
  module, not a separate product.
- As built in M3: file sources sync on open and on schedule; other sources push
  documents through the admin API. Retrieval is keyword scoring until embeddings
  arrive with pgvector (decision 46). PDF, DOCX and OCR need the `documents` pack.

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

Every pack ships an eval set in **YAML**: scripted conversations and tasks,
one case per document, with expected outcomes and tool calls. Evals run offline against `FakeProvider` and against
real models before any model swap, pack upgrade or config release. Results are
stored so behavioural drift is visible over time.

As built in M3 (decision 48), `dif-general-harness eval INSTANCE` runs every case in a fresh
instance under the headless service. A case can start triggers, advance the clock, and check
templates, messages, handoffs, approvals and tool calls; `must_not` rules count unsafe
actions, and replies can be judged by category with the `verifier` role. Each run is
recorded in the instance's database, and cases that passed in the previous run and fail now
are reported as regressions.

### 3.22 Fleet operations: control plane and instance agent

Di-Factory runs, adjusts and upgrades many client instances, each in a
different client cloud:

- **Instance agent:** a small component inside every deployment. Its
  connections are **outbound only**. It reports health, metrics, costs and eval
  results, and it **pulls** approved config versions and pack upgrades.
  Nothing reaches into the client's cloud. The client can revoke it at any
  time, and the instance keeps running without it.
- **Control plane** (Di-Factory side):
  - a fleet view across clients (health, costs, eval drift, escalation
    rates);
  - a pack-upgrade rollout that goes instance by instance, gated by each
    instance's evals, with rollback;
  - remote config changes, applied only through the instance's normal
    versioned-config path;
  - an audit record of every change.
- It never reads raw client data. It receives only aggregates, and redacted
  traces when the client allows them.

### 3.23 Language

**English is the default.** Pack prompts, templates, docs and the console are
written in English. Spanish (es-MX) is added only when a client asks for it: the
instance sets `solution.locale`, agents reply in that language, and any
customer-facing templates are overridden in the instance with the client's
wording.

## 4. Tech stack

| Concern | Choice |
|---|---|
| Python | 3.12+ · MIT · distribution name `di-factory-general-harness` (not published to PyPI; decision 22) |
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
  fleet/       instance_agent.py  (the control plane is a separate component)
  governance/  pii.py consent.py audit.py retention.py region.py
  tenancy/     tenant.py config_versions.py secrets.py
  store/       jsonl.py sqlite.py postgres.py
  service/     app.py (FastAPI: channels, webhooks, admin, inbox)
  console/     tui/ (Textual)  cli.py
  constructor/ catalog.py interview.py build.py verify.py deploy.py
  routing.py   checkpoints.py  telemetry.py
packs/         reusable solution packs (spec fragments + prompts + evals), incl. the constructor pack
integrations/  openclaw-skill/ (thin wrapper so Teky can drive the constructor)
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
   - **TUI console** for building and testing instances locally;
   - **constructor v1:** pack matching, interview from pack questionnaires,
     instance-spec generation, validation and offline evals.
3. **M2 – Headless runtime and governance minimum:**
   - FastAPI service;
   - channel adapters (a messaging gateway first) and triggers (scheduler,
     webhooks);
   - durable queue;
   - Postgres backend;
   - approvals inbox and escalation;
   - secrets vault adapters;
   - versioned config;
   - **instance agent** (health, metrics, pulls approved config);
   - Docker image;
   - **basic AWS deploy** (minimal Terraform: one container, Postgres,
     Secrets Manager, single region);
   - **constructor v2:** approval gate and deploy to Docker or basic AWS;
     Teky wrapper skill.
   - PII tokenization, consent/opt-out, audit log and retention.
4. **M3 – Intelligence modules:**
   - 5-layer memory;
   - knowledge/RAG;
   - verification (both tiers);
   - feedback loop;
   - agent teams and workflows;
   - evals per solution.
5. **M4 – Operations hardening:**
   - Terraform hardening (sizing profiles, upgrades, rollback);
   - **constructor v3:** full lifecycle (adjust, upgrade, control-plane
     registration);
   - OpenTelemetry;
   - cost reports per tenant and vendor;
   - provider-region policy;
   - ContainerExecutor;
   - **control plane MVP** (fleet view, gated pack rollouts, remote config);
   - GCP/Azure profiles.

Milestones have **no dates** (decision 28): each one is done when its gate
passes, and work moves straight on to the next.

**Status:** M0 to M4 are done; each gate is `tests/test_acceptance_m<n>.py` (M4's is the
v1.0 gate). After v1.0, the v1.0 known gaps were closed (decisions 58–64, each with its
tests); next are the GCP and Azure profiles and the remaining gaps listed in `CLAUDE.md`.

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
| 7 | Deployment | **AWS first**: Docker image + Terraform (basic in M2, hardened in M4); GCP/Azure later. Local mode for development. |
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
| 22 | Packaging | Python 3.12+, MIT, distribution `di-factory-general-harness`, import `dif_general_harness`, CLI `dif-general-harness`. **Not published to PyPI** (revised 2026-09-29): releases are git tags, installed with `uv tool install git+<repo>@<tag>`, and client deployments ship as container images (M2). PyPI stays an option if the open core needs outside adoption; no code changes required. |
| 23 | Pack licensing | **Open core**: core + reference packs MIT; production packs are Di-Factory proprietary; each client fully owns its instance. |
| 24 | Pack sequence | Appointment Agent first, then **Service Desk cell**, **Conversational RAG assistant** and the **other PyME agents**; **Dev cell after v1** (needs the M4 container executor). |
| 25 | Messaging gateway | **Client holds the account** (zero markup, client owns numbers and templates); Di-Factory sets it up. |
| 26 | Success targets | ≥80% reuse · ≤2 weeks onboarding on an existing pack · ≤4 weeks per new pack · ≥90% eval pass · 0 unsafe actions · 0 leaks · 100% cost attribution. |
| 27 | State directory | Project state in **`.dif/`**, user state in **`~/.dif/`**, project memory file **`DIF.md`**. |
| 28 | Timeline | **No time goals.** Milestones are ordered and gated by their acceptance tests; owner Jag Pascoe with agent builders. |
| 29 | Fleet operations | **Control plane + outbound-only instance agent** that reports health, cost and evals and pulls approved config and pack upgrades; the client can revoke it. §3.22 |
| 30 | Spec portability | **Harness-native**, kept clean so exporters to other platforms can come later; no cross-platform promise in v1. |
| 31 | Delivery agents | Internal ones stay on OpenClaw/Paperclip; **client-facing ones run on the harness** in the client's cloud. |
| 32 | Paper test 2 | Before M0, also test the spec on a **batch document job** (Receipt Processing), a **Dev cell** and an **OPC-style agent team**. |
| 33 | Condition language | **CEL subset** for workflow branches, escalation rules and trigger filters. |
| 34 | Language | **English by default**; Spanish (es-MX) only when a client asks. §3.23 |
| 35 | Eval format | **YAML**, one case per document; specs stay JSON. |
| 36 | Constructor agent | Ships **with the harness** (`dif-general-harness build`) as a harness pack, plus a thin **OpenClaw skill** so Teky can drive it. Flow: intake → match → interview → build → verify → approve → deploy → hand over. §0.4 |
| 37 | Deploy approval | **Jag approves every deployment** into a client cloud; client sign-off happens outside the tool. |
| 38 | Constructor timing | **Incremental:** v1 in M1 (interview, build, validate, evals), v2 in M2 (approval gate, deploy to Docker or basic AWS, Teky skill), v3 in M4 (full lifecycle). |
| 39 | First-client deploy | **Basic AWS deploy moves to M2** (minimal Terraform); M4 hardens it. |
| 40 | Paper test 2 result | The spec covers batch, coding and agent-team shapes after the additions in SOLUTION_SPEC §5.14–5.16. The Dev cell needs the container executor, which **stays in M4**; the Dev cell pack ships after v1. |
| 41 | Deploy approval (M2) | Jag's approval is an **Ed25519 signature over the content hash of the staged solution** (instance, overrides, every pack file) for one target. Any change after approval, even one prompt line, invalidates it; the deploy refuses keys not in the trusted approvers list. The control plane signs pushed config the same way (data + approver). |
| 42 | Headless approvals (M2) | **Deferred, not blocking:** a tool call that needs approval files an inbox item and returns "waiting for approval"; on approval the stored call runs (deny rules re-checked) and a follow-up turn tells the contact. No model call is held open; `hitl.on_timeout` applies when nobody decides. |
| 43 | Conditions (M3) | Spec conditions (`when`, `unless`, `expr`, escalation rules) are a **small CEL subset**: no function calls, bounded length and depth. A missing field is falsy, so a condition never passes by accident. |
| 44 | Verification (M3) | Checks run **before** the approval step. A second failure on the same call refuses it unchecked from then on, applies the escalation rules and proposes a constraint. A check that cannot run here turns the tool to `ask`; it never lets the call through. |
| 45 | Memory storage (M3) | Episodic, semantic and procedural records live in the instance database, scoped by tenant, instance and **memory scope** (contact, instance or agent). The working layer is the context window; forgetting is TTLs, retention and a per-contact delete. A changed fact supersedes the old one and goes to the inbox; a denial restores it. |
| 46 | Retrieval (M3) | Keyword scoring whose score is the **share of the query's information a passage covers (0–1)**, so `min_score` means the same in every corpus; BM25 breaks ties. Embeddings (hybrid, pgvector) come with production Postgres in M4. Citations are enforced by a check with **one rewrite, then "not found"**. |
| 47 | Feedback (M3) | Signals (a check failing twice, a denial or escalation resolved with a reason, a budget stop, a thumbs-down with a comment) only **propose** constraints. A person approves them, optionally reworded; approved ones are pinned in the prompt until retired. |
| 48 | Evals (M3) | Each case runs in a **fresh instance under the headless service**: channels record, fixtures replace connectors, approvals are never granted, and a case controls the clock. Results are stored per config version; a case that passed before and fails now is a **regression**. |
| 49 | Python extensions (M3) | `tools.python` loads in-process from the **staged solution**, so Jag's deploy signature covers the code. The tool's namespace is the module's name. Extensions run behind the same permissions, verification, budgets and audit; isolating them in a container waits for the M4 executor. |
| 50 | Email and Slack (M3) | Email arrives through the provider's **inbound-parse webhook** (no IMAP polling) and leaves through SMTP; Slack uses the Events API with one session per thread. |
| 51 | Cost ledger (M4) | Every model call is recorded **once** in a usage ledger (day, agent, role, vendor, model: calls, tokens, USD at list price) through one charge path; reports group by any of those, and models without a price are listed as **unpriced**, never reported as free. |
| 52 | Telemetry (M4) | A small, dependency-free **OTLP/HTTP exporter** in the GenAI conventions (`invoke_agent`, `chat`, `execute_tool`), on only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. **No content leaves**: names, counts, timings and costs only; a collector that is down never slows or breaks a run. |
| 53 | Region policy (M4) | Providers declare a `region` (the direct Anthropic API defaults to `us`); `governance.regions.models` and `models.allowed_regions` both apply; a provider with no known region is refused while a policy is set; later layers can only narrow the list and the data region cannot move. |
| 54 | Container executor (M4) | Shell commands run in a **throwaway container per command**: only the workspace mounted, no network unless an egress proxy enforces `allow_hosts`, no capabilities, a read-only root, resource limits, the entrypoint forced to bash, killed on timeout. A bad executor config means no shell, never an unconfined one. |
| 55 | AWS module (M4) | Sizing **profiles** (small, medium, large: task count, database class, Multi-AZ, backups, private tasks), rolling upgrades with the circuit breaker, images tagged `<version>-<solution hash>` and kept for rollback, CloudWatch alarms to SNS. The module ships `terraform test` files that plan the profiles against a mocked provider. |
| 56 | Control plane (M4) | Instances pull **signed offers** (a config, or a rollback to a hash they ran), where the signature covers data, approver and gate. An **eval-gated** offer activates only after the instance runs the offered config's suites with its own models (a gate that runs nothing fails). **Rollouts** go instance by instance and roll back every upgraded instance, to the config it reported running before, when one rejects. The control plane validates offers except for file existence, which the instance checks. |
| 57 | Constructor v3 (M4) | `adjust` and `upgrade` re-resolve, validate and **diff** an instance before writing it; offers carry the config **as the container resolves it** (paths under `/app/solution`); a change that needs new files ships as a new deploy. Instance values are checked against their variables (`invalid_value`), not only in the interview. |
| 58 | Context (after v1) | **Compaction** runs before a turn when the history exceeds the agent's `context_tokens` and a `compaction` role exists: facts are extracted to memory first, the tail from a user message is kept verbatim, and a `context_compacted` event makes resumption exact. The **router short-circuit** answers only listed intents for contact messages; it can save work, never decide, and any doubt falls through to the main agent. |
| 59 | File and batch triggers (after v1) | File triggers **poll** a folder or an S3 bucket (no bucket notifications to configure in the client's account) and remember, per trigger, each object version they read and each `dedupe_key` value they fired for, so a copy or re-upload never starts work twice, even after the job history is purged (the memory follows `documents` retention). Batch sources are **read tools only**, called under the same permissions; pushed items need no source. |
| 60 | Documents (after v1) | Text layers are read **locally** (pypdf for PDF; DOCX and XLSX with the standard library, DTDs refused); a document with no text layer is flagged `needs_ocr` and read by a **vision model role** (`ocr`, else `main`), charged and region-checked like any call, rather than a Tesseract install in every image. Tools read only the solution's own sources, by `uri`. Knowledge file sources index PDF, DOCX and XLSX text; scans stay reported as skipped. |
| 61 | Sampled verification (after v1) | `sampled` picks calls and answers by a **hash of the session and the call or turn**, so retries and replays decide the same way. Side effects are checked before they commit; **answers are reviewed after sending**, because a gate on every sampled answer would add latency to a random share of contacts, and a wrong answer is fixed by a person (the `review` inbox item) and at the source (a proposed constraint). |
| 62 | Egress and isolation (after v1) | The **egress proxy runs inside the harness**, which already joins the containers' internal network: no extra service to deploy, and it knows each command's `allow_hosts`, so every command gets **its own credentials**, revoked when it ends. The proxy resolves hosts itself and connects to the checked address (no DNS rebinding into the client's network). **Isolated extensions** are described with `ast`, never imported by the harness, and run per call in the same container executor; a bad isolation config turns extensions off, never on unconfined. |
| 63 | Hybrid retrieval and remote sources (after v1) | Vectors live in a **portable table** (SQLite and Postgres alike) and are compared in-process, exactly, with results fused by **reciprocal rank fusion**; `min_score` keeps its meaning and `min_similarity` adds a semantic floor. This covers SMB corpora (up to about 100k chunks) without pgvector, which remains an option for larger ones. Embeddings come from an `embedding` role on an OpenAI-compatible endpoint, charged like any call; a down model degrades to keywords. S3, Google Drive and web pages sync incrementally by source version; a source that fails to list deletes nothing. |
| 64 | Voice (after v1) | Phone calls use **the provider's own speech recognition and text-to-speech** (Twilio `<Gather input="speech">` and `<Say>`), so the harness stays text-only: each utterance is an ordinary inline turn with every guardrail, and no audio is stored or streamed through the harness. Streaming speech-to-speech (barge-in, lower latency) is a later option behind the same channel type. |
| 65 | Web chat (after v1) | The instance serves its own chat page (`/chat`) for a `web` channel, so a client needs no separate front end. Private by default (a bearer access code); `"public": true` opens it to anyone with the link, which only the layer declaring the channel may set (opening a channel is a loosening, so the merge refuses it from a later layer). A public chat identifies each browser by a random 128-bit id, rate-limits per visitor and per address, and relies on the budgets for spend; later replies (a person, a reminder) wait in an in-memory outbox the page polls. The same instance serves a landing page at `/` built only from the client's FAQ answers, so the page and the agent always say the same thing. |
| 66 | Guided setup advisers (after v1) | The setup asks a small model to recommend packs and name the needs no pack covers (recorded as Di-Factory design work, never improvised), and, on request, a top model as business consultant: one follow-up per thin business answer and recommendations at the end. Both are advice only: the pack choice, the answers and Jag's signature stay with people, and the setup works without either model. |
| 67 | Small corpora are read whole (after v1) | Word matching fails a business FAQ in the ways that matter most: the client answers in one language and patients ask in another, or in other words. Below `retrieval.read_whole_below` characters the search returns the whole corpus (matches first) and the model decides, still only from the documents and still under `not_found`. Larger corpora keep scored retrieval; hybrid retrieval remains the answer there. |
| 68 | The client's look, from any material (after v1) | A landing page is the client's shop window, so it carries their brand: `branding` (primary and accent colors, a logo). People rarely have a palette in hex, so the setup accepts what they do have — logos, photos, brand guides, slide decks, CSS — and reads colors from pixels, text and Office themes, preferring written colors over sampled ones and never choosing a grey. The logo is stored inline and small so the signed solution is the whole truth; remote assets are refused. Missing keys are explained the same way: where each one is used and what stays off without it. |
| 69 | Handing a solution to its client (after v1) | A client who runs their assistant with their own Claude gets a folder of documents, not a walkthrough: `CLAUDE.md`, the business from their own answers, the solution, routines, what is missing and its impact, what they change and what Di-Factory changes, a one-page guide in their language, and one command (`admin`) for the running instance. Content (the FAQ) is the owner's to change without a signature, through the admin API, and their edit stands until a new signed release of that file; structure (pack, prompts, rules, channels, budgets) stays signed by Di-Factory, and the handover refuses to pass while an approver key is on the client's server. The handover is the deliberate last step (setup → fine-tuning rounds → handover), not part of every deploy; the `/handover` skill leads it: the client's own keys, the documents, Jag's key off the server, Di-Factory's access removed last. Claude's permissions ask before anything reaches a customer or the FAQ, and deny secrets, Docker and the harness. |
| 70 | Text from outside is data (after v1) | A public chat and a crawled site make prompt injection cheap to try, and every web page, document, knowledge passage or API body reaches the model inside a tool result. Such text is wrapped in `<untrusted_content>` markers (look-alike markers inside it are defanged, so it cannot close the fence), every agent's system prompt says nothing inside them is an instruction, and a pattern check (English and Spanish phrasings, role spoofing) flags suspicious documents when they are indexed (`knowledge_suspicious`, audited) and leaves such lines out of a site before the writer model sees it. Flagging is a warning for a person, never a filter the agent relies on: the fence is the defence. (After *Harness Engineering*, Barbaste et al. 2026: untrusted-content delimiting.) |
| 71 | Cheap caps on a run (after v1) | Turn and cost budgets do not catch a run that loops cheaply. The loop also ends a run as `stuck` when the same tool call with the same input comes a fourth time, or when every tool call failed three turns in a row, and as `timeout` past one reply's wall-clock limit (240 s, `max_seconds` per agent), checked between model calls. Both go to a person like a budget stop. A dozen lines, no stuck-detection model: what production harnesses ship as the floor. |
| 72 | What the FAQ lacks is a list (after v1) | Every question the documents did not answer (nothing found, or an answer with no source left after the citations check) is kept with the contact's own words, as stored (PII tokenized), and counted again when asked in other words of the same terms. The owner (or their Claude: `./negocio faq gaps`, skill `preguntas`) adds the answers and marks each one answered or dismissed; an answered one that is asked again opens again. Fine-tuning works from that list instead of guessing. Kept as long as conversations. |
| 73 | Compaction when the model refuses the history (after v1) | The compaction threshold is an estimate (characters / 4), so a long conversation can still exceed the model's context. Providers translate their "prompt is too long" errors into a neutral `ContextOverflow`; the loop ends that run as `overflow` and can continue a history without adding a message; the agent then compacts at once to half the history's size (the same routine: facts to memory first, the recent tail verbatim, the summary merged with the earlier one) and retries the turn once. Without a `compaction` role, or with nothing to cut, the turn goes to a person like any other stop. |
| 74 | The business's published details are not private (after v1) | PII tokenization hid the business's own email and phone from its customers: they came from its knowledge documents, were tokenized like a customer's, and only `reveal_in_output` classes were shown, so the assistant said the contact details were masked. Tokens found in knowledge results are now marked published: the model still only sees tokens (nothing changes for the provider), the reply shows their values, and the result tells the model to write them as they are. What a customer types stays masked as before. A written-up site or a small document set is also read whole (`read_whole_below`, decision 67), so a question in another language than the documents still finds its answer without an embedding model. |
| 75 | Real conversations replayed on a rebuild (after v1) | A fine-tuning round changes answers, settings or the pack, and evals only cover the cases someone wrote. Before a rebuilt client goes online, its latest real conversations (from the running instance: `GET /admin/sessions`) are sent again, turn by turn, to the new version, each in a throwaway instance run like an eval case (channels record, approvals are never granted, nothing reaches a customer or commits a side effect). The pack's `verifier` role judges each new reply against the one the customer got (same, better, worse, changed; identical replies need no model); the report lists every turn before and after, worse first, and a worse reply makes the setup ask before putting the rebuild online. A crashed replay is reported, never counted as same. (After *Harness Engineering*, Barbaste et al. 2026: session replay as the check that a change helped.) |

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
