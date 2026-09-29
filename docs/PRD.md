# PRD — dif-general-harness

2026-09-27 · Jag Pascoe · Snapshot of the living PRD at
<https://claude.ai/code/artifact/786caf13-560e-4a91-aa7a-775a14d99d03>
(the live doc has the architecture and roadmap diagrams). Technical design:
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Overview

**dif-general-harness is Di-Factory's general solution template: an open-source (MIT) agent runtime with pluggable capability modules. Every client solution a line of business sells is delivered as an instance of it.**

**Context.** Di-Factory (∂i~ƒ, Data Intelligence Factory, CDMX) sells seven lines of business: Tailored ML Models, MVP Development, Business Consulting, VC Partnership, Agentic Transformation (Dev, Ops and Service Desk cells), OPC Startup and SMB/PyME Solutions. Di-Factory itself runs on OpenClaw with Paperclip. The harness is separate from that: it is one base platform among several, used to run, adjust and deploy client solutions across every line of business where it applies. Di-Factory's internal delivery agents stay on OpenClaw/Paperclip; client-facing ones run on the harness.

**Problem.** Each client solution is rebuilt from scratch: agents, channels, connectors, memory, safety and compliance. That makes fixed-price, fixed-time delivery fragile and cuts margins. The PyME promise "80% is already built, the 20% is yours" needs an actual 80% in code.

**Vision.** One template (core runtime, capability modules, governance and operations) and a declarative solution spec per client. Onboarding a client means writing a spec override and connecting secrets, not writing new code. It runs in the client's cloud, the client owns everything, there is no lock-in to one model or cloud, and third-party costs pass through at zero markup.

**How it is used.** When Di-Factory sells solution S to client X, Jag (or Teky, Di-Factory's CTO agent on OpenClaw) asks the constructor agent to build it. The constructor matches S to the pack catalog, interviews for the open details, writes the instance spec, verifies it with evals, and after Jag's approval deploys it into X's cloud.

**Summary.** A headless runtime (channels, triggers, admin and approvals API) with a terminal console for developers and operators. It is model-agnostic from the first usable release and deploys to AWS first. It is distributed as `di-factory-general-harness` through git release tags and, for client deployments, container images; it is not published to PyPI (command `dif-general-harness`, import `dif_general_harness`).

## Goals and non-goals

v1 delivers a template good enough that a Di-Factory solution can be built by writing a spec, not a codebase, and run safely in a client's cloud.

**Goals (v1)**

1. A solution-spec format (agents, models, tools, knowledge, channels, triggers, workflows, policies, governance, evals, deploy) with reusable packs and client instances.
2. A headless runtime: channels, triggers, durable workflows, approvals inbox and escalation, with a terminal console for building and testing instances.
3. Model-agnostic from the first usable release: Anthropic plus OpenAI-compatible adapters, conformance-tested.
4. Tools as contracts: registry, MCP, HTTP connectors and tool packs, with structured observations and verification of side effects.
5. Tenant-scoped everything; single-tenant per client cloud by default, multi-tenant capable.
6. Governance for Mexican regulations: PII tokenization, consent and opt-out, immutable audit log, retention.
7. Intelligence modules: 5-layer memory, knowledge/RAG, verification, feedback loop, agent teams.
8. Safe and accountable operation: budgets, permissions, secrets in client vaults, cost per tenant and vendor, evals per solution.
9. Deployable to AWS with a container image and Terraform; local mode for development.

**Non-goals (v1)**

- Running Di-Factory itself (OpenClaw and Paperclip keep that role).
- ML training pipelines (the ML Loop stays separate; models plug in as tools).
- A web-app framework (MVPs stay on Django).
- A no-code or visual solution builder.
- Voice channels, and GCP/Azure deployment modules.
- CNBV and NOM-024 compliance profiles (after the LFPDPPP baseline).
- Automatic model routing by a task classifier.

## Users and lines of business

Four personas use the template; seven lines of business consume it as instances.

| Persona | Who | What they do with the harness | What they need most |
| --- | --- | --- | --- |
| Solution builder (primary) | Di-Factory engineer (or agent) delivering a client solution | Writes packs and instance specs, connects tools, runs evals, deploys | Clear spec format, reusable packs, console, fast local loop |
| Client operator | Clinic, SMB or enterprise staff running the solution | Approves actions, handles escalations, reviews results | Approvals inbox, notifications, simple dashboards |
| End user (contact) | Patient, customer, employee talking to the agent | Chats over WhatsApp, Telegram, web or email | Fast, correct answers; privacy; a path to a human |
| Client owner / auditor | Person accountable for data and cost | Audits actions, costs and data handling | Audit log, cost per vendor, retention, ownership of everything |

**Line of business → capability modules** (● required, ○ optional, – not needed)

| Module | PyME agents | Agentic cells | RAG assistant | ML models | Client-facing delivery agents (MVP, Consulting, VC) | OPC (on request) |
| --- | --- | --- | --- | --- | --- | --- |
| Core loop, providers, routing, budgets | ● | ● | ● | ● | ● | ● |
| Solution spec | ● | ● | ● | ● | ● | ● |
| Channels | ● | ● | ● | ○ | – | ● |
| Triggers | ● | ● | ○ | ○ | ○ | ● |
| Tool registry + MCP connectors | ● | ● | ● | ● | ● | ● |
| Coding tool pack | – | ● (Dev) | – | – | ● (MVP) | ○ |
| Knowledge / RAG | ○ | ● | ● | – | ● | ○ |
| 5-layer memory | ● | ● | ○ | ○ | ● | ● |
| Agent teams | ○ | ● | – | – | ○ | ● |
| Durable workflows | ● | ● | ○ | ○ | ○ | ● |
| Approvals inbox + escalation | ● | ● | ● | ○ | ● | ● |
| Verification | ● | ● | ● | ● | ● | ● |
| Governance | ● | ● | ● | ● | ○ | ○ |
| ML models as tools | ○ | ○ | – | ● | – | ○ |
| Evals per solution | ● | ● | ● | ● | ● | ● |
| Tenancy, config, secrets, deploy | ● | ● | ● | ● | ○ | ● |

## User stories

Sixteen stories define the template; each maps to requirements in the next section.

| # | As a… | I want… | So that… |
| --- | --- | --- | --- |
| US-1 | solution builder | to start a client solution from a pack and write only an override spec | a new client takes days, not weeks |
| US-2 | solution builder | to run and debug an instance locally in the console with a fake or real model | I can iterate before deploying |
| US-3 | solution builder | to switch models per role in the spec and prove it with the solution's evals | model choice stays with the client, backed by evidence |
| US-4 | solution builder | to add a tool by declaring its contract (MCP or HTTP) | new capabilities need no agent code |
| US-5 | solution builder | to deploy an instance to the client's AWS account with one command | the client owns cloud, data and keys |
| US-6 | end user | to talk to the agent on the channel I already use and reach a human when needed | I get help without changing apps |
| US-7 | client operator | scheduled and event-driven work to run without anyone watching | reminders and follow-ups happen on time |
| US-8 | client operator | risky actions to wait in an approvals inbox | nothing important happens without my consent |
| US-9 | client operator | escalations to arrive with the full conversation, plan and trace | I can resolve them without re-asking |
| US-10 | client owner | personal data tokenized before it reaches the model, with consent and retention enforced | we comply with LFPDPPP |
| US-11 | client owner | an audit log and cost report per vendor | I can verify what was done and what it cost, with zero markup |
| US-12 | client operator | the agent to remember each contact and past outcomes | conversations continue where they left off |
| US-13 | solution builder | agents to answer from the client's documents with citations | answers are grounded, not invented |
| US-14 | solution builder | multi-step work to survive restarts and retries | long workflows never lose their place |
| US-15 | Di-Factory operator | to see every client instance's health and cost, and roll out pack upgrades safely | I can run, adjust and upgrade many clients without touching their data |
| US-16 | Di-Factory (Jag or Teky) | to ask the constructor agent to build solution S for client X, answer its questions, approve, and have it deployed in X's cloud | a new client goes from sale to running solution with configuration, not new code |

## Functional requirements

P0 = required for the milestone; P1 = planned for v1; P2 = after v1. Milestones are defined under Milestones below.

| ID | Area | Requirement | Priority | Milestone | Stories |
| --- | --- | --- | --- | --- | --- |
| FR-1 | Core | Async agent loop with streaming, parallel tool calls and stop conditions; provider-neutral message and event model | P0 | M0 | US-2 |
| FR-2 | Spec | Solution-spec schema and loader (agents, models, tools, knowledge, channels, triggers, workflows, policies, governance, evals, deploy); packs and instance overrides | P0 | M0 | US-1 |
| FR-3 | Tenancy | Tenant and instance ids on every record; single-tenant default, multi-tenant capable | P0 | M0 | US-10 |
| FR-4 | Providers | Anthropic (direct, Bedrock, Vertex) and OpenAI-compatible adapters; FakeProvider; shared conformance suite | P0 | M1 | US-3 |
| FR-5 | Routing | Per-role models (main, subagent, verifier, compaction, memory_extraction, router, title), set in the spec | P0 | M1 | US-3 |
| FR-6 | Tools | Registry of JSON-schema contracts; Python tools, MCP servers, HTTP connectors; declared effect (read / write / external); structured observations | P0 | M1 | US-4 |
| FR-7 | Tools | Tool packs: coding, general, connectors (calendar, CRM, messaging, spreadsheets), ml (model endpoints) | P1 | M1–M3 | US-4 |
| FR-8 | Safety | Permission policy (allow / ask / deny) by tool, arguments and effect; budgets per run and per tenant per day; secret redaction and deny list; checkpoints | P0 | M1 | US-8 |
| FR-9 | Console | Textual console to run, inspect and replay instances locally | P0 | M1 | US-2 |
| FR-10 | Runtime | Headless service (FastAPI) with admin API and webhook endpoints | P0 | M2 | US-7 |
| FR-11 | Channels | Canonical message envelope; adapters for a messaging gateway (WhatsApp/SMS), Telegram, web widget, email; conversation key per contact | P0 | M2 | US-6 |
| FR-12 | Triggers | Cron schedules, delayed jobs, webhooks from client systems, batch runs | P0 | M2 | US-7 |
| FR-13 | Workflows | Durable queue (Postgres-native) with retries, timeouts, waits for approvals and events; persisted, replayable plans | P0 | M2 | US-14 |
| FR-14 | HITL | Approvals inbox (API, console, notifications); escalation with full trace to a person or helpdesk | P0 | M2 | US-8, US-9 |
| FR-15 | Governance | PII tokenization before the model; consent and opt-out; immutable audit log; retention per data class | P0 | M2 | US-10, US-11 |
| FR-16 | Platform | Postgres backend; secrets vault adapters; versioned, hot-reloadable config with rollback; Docker image | P0 | M2 | US-5 |
| FR-17 | Memory | 5-layer memory scoped per tenant, instance and contact, with retrieval at session start and memory tools | P0 | M3 | US-12 |
| FR-18 | Knowledge | Ingest documents and connectors with provenance and versioning; hybrid retrieval; cited answers; "not found" below threshold | P0 | M3 | US-13 |
| FR-19 | Verification | Deterministic checks plus an optional verifier agent; critical side effects verified before commit | P0 | M3 | US-8 |
| FR-20 | Teams | Named long-lived agents with roles, handoffs, shared task ledger and sub-agents | P1 | M3 | US-14 |
| FR-21 | Feedback | Candidate constraints and skills proposed via the inbox; approved ones pinned | P1 | M3 | US-8 |
| FR-22 | Evals | Eval set per pack; run offline and against real models before model swaps and releases | P0 | M3 | US-3 |
| FR-23 | Deploy | Hardened Terraform for AWS (sizing profiles, upgrades, rollback); one-command deploy into the client account | P0 | M4 | US-5 |
| FR-24 | Observability | Cost per tenant, vendor and role; quality metrics; OpenTelemetry export | P1 | M2 (cost), M4 (OTel) | US-11 |
| FR-25 | Governance | Provider-region policy; CNBV and NOM-024 profiles | P2 | M4+ | US-10 |
| FR-26 | Channels | Voice (speech-to-text and text-to-speech); Slack | P2 | after v1 | US-6 |
| FR-27 | Fleet | Instance agent (outbound only): reports health, metrics, costs and eval results; pulls approved config and pack upgrades; revocable by the client | P0 | M2 | US-15 |
| FR-28 | Fleet | Control plane MVP: fleet view across clients, pack upgrades rolled out instance by instance and gated by evals, remote config through versioned config, audit of every change | P1 | M4 | US-15 |
| FR-29 | Constructor | v1: match a request to packs, interview from pack questionnaires (`variables[*].ask`), write the instance spec, validate, run offline evals | P0 | M1 | US-16 |
| FR-30 | Constructor | v2: approval gate (Jag), deploy to Docker or basic AWS, client-held secrets; OpenClaw skill so Teky can drive it | P0 | M2 | US-16 |
| FR-31 | Constructor | v3: adjust and upgrade live instances through the same flow; register with the control plane | P1 | M4 | US-15, US-16 |
| FR-32 | Deploy | Basic AWS deploy (minimal Terraform: container, Postgres, Secrets Manager, one region) | P0 | M2 | US-5 |

## Non-functional requirements

Safety, ownership and predictability outrank speed; every target below is testable.

| ID | Category | Requirement |
| --- | --- | --- |
| NFR-1 | Safety | No write or external side effect without an allow rule or approval; prompt-injected content can never bypass the permission check |
| NFR-2 | Privacy | Raw PII never reaches the model when tokenization is on; secrets never appear in logs, traces or memory (tested with planted values) |
| NFR-3 | Isolation | No data crosses tenants, instances or contacts (isolation tests on every store) |
| NFR-4 | Ownership | Everything runs in the client's cloud with client-owned keys; the solution keeps working without Di-Factory |
| NFR-5 | Portability | Model, provider and cloud are configuration; no vendor SDK types in the core; MIT license |
| NFR-6 | Reliability | Workflows survive process restarts; a crash loses at most the in-flight step; every tool call ends in a structured observation |
| NFR-7 | Performance | Harness overhead under 50 ms per step, excluding model and tool time; first response on a channel under 5 s at p95 |
| NFR-8 | Performance | Memory and knowledge retrieval under 500 ms with 10,000 items per tenant |
| NFR-9 | Cost | Prompt caching on stable prefixes; intent short-circuit for trivial requests; token budgets on memory and pinned blocks |
| NFR-10 | Auditability | Every action traceable to tenant, instance, agent, plan, tool call and approver; audit log is append-only |
| NFR-11 | Operability | Config changes are versioned with rollback and need no redeploy; health checks and runbook per instance |
| NFR-12 | Maintainability | Strict mypy on the core; ruff clean; unit tests offline with FakeProvider; conformance and eval suites in CI |
| NFR-13 | Localization | English by default for prompts, templates, docs and console; Spanish (es-MX) added only when a client asks |

## Architecture and key decisions

We own the agent loop and keep adapters thin; frameworks (LangGraph, Pydantic AI) and wrapping a vendor SDK were rejected because they hide the loop and conflict with the no-lock-in promise. Full design: [`ARCHITECTURE.md`](ARCHITECTURE.md).

Layers, top to bottom (diagram in the live doc): **Surfaces** (channels and triggers, admin API with approvals inbox, TUI console, Python API) → **Instance** (solution spec to tenant-scoped instance) → **Core** (agent loop, prompt builder, context manager, permissions, guardrails and budgets, verifier, workflow engine) → **Modules** (providers, tools and MCP, knowledge, memory, agent teams, HITL inbox, ML models, evals) → **Governance** (PII tokens, consent, audit log, retention, region policy) → **Platform** (storage, secrets, executor, deploy).

A channel message, schedule or console command reaches a tenant-scoped instance built from its spec. The core runs the loop under the instance's policies and reaches modules, governance and platform services only through adapters.

| Area | Decision |
| --- | --- |
| Product scope | General solution template for Di-Factory LOB solutions; instances built on it |
| OpenClaw / Paperclip | Separate: they run Di-Factory; the harness is one delivery option for client solutions |
| Primary surface | Headless runtime first; TUI is the developer and operator console |
| Providers | Anthropic + OpenAI-compatible from M1, conformance-tested |
| Tenancy | Single-tenant per client cloud by default, multi-tenant capable |
| Governance | Minimum set (PII tokens, consent, audit, retention) before the first client instance |
| Deployment | AWS first: Docker + Terraform; local mode for development |
| Multi-agent | Orchestrator + sub-agents, plus agent teams with roles and handoffs |
| Config | JSON specs and settings, versioned and hot-reloadable |
| Secrets | By reference through client vault adapters, never in specs |
| Permissions | Reads allowed; writes and external side effects ask; headless asks go to the inbox |
| Verification | Deterministic checks + optional verifier; critical side effects verified before commit |
| Memory | 5 layers per tenant, instance and contact; SQLite locally, Postgres in production |
| Budgets | On by default per run and per tenant per day |
| Evals | Every pack ships an eval set; run before model swaps and releases |
| Packaging | Python 3.12+, MIT, `di-factory-general-harness`; git release tags and container images, no PyPI |

## Success metrics and acceptance tests

The template succeeds when new client solutions are mostly configuration and run safely in client clouds. Targets were confirmed in review on 2026-09-27.

**Business and product metrics**

| Metric | How it is measured | Target |
| --- | --- | --- |
| Reuse ratio | Share of an instance's code and config that comes from the template and packs | ≥ 80% |
| Time to onboard a client on an existing pack | Kickoff to running instance in the client's cloud | ≤ 2 weeks |
| Time to build a new pack | Pack spec, connectors and evals ready | ≤ 4 weeks |
| Unsafe actions without approval | Side effects taken without an allow rule or approval | 0 |
| PII or secret leaks | Planted PII or secrets found in model inputs, logs, traces or memory | 0 |
| Eval pass rate per pack | Pack eval set against the configured models | ≥ 90% before release |
| Cost transparency | Share of third-party cost attributed to tenant and vendor | 100% |

**Acceptance tests (template level)**

- [ ] **Spec:** a pack plus an override spec loads, validates and runs with no code changes.
- [ ] **Model swap:** changing a role's model in the spec switches providers, and the pack's evals decide pass or fail.
- [ ] **Tool contract:** adding an MCP or HTTP tool to the spec makes it callable without redeploy.
- [ ] **Permissions:** a prompt-injected message asking for a side effect produces an approval request, never execution.
- [ ] **Headless:** a scheduled trigger and an inbound channel message each run a turn and reply through the channel.
- [ ] **Durability:** killing the service mid-workflow and restarting resumes from the last completed step.
- [ ] **Escalation:** an escalated conversation arrives in the inbox with transcript, plan, tool trace and verdict.
- [ ] **PII:** planted names, phones and CURP never appear in model inputs; replies show them only where allowed.
- [ ] **Consent:** an opted-out contact receives no triggered messages.
- [ ] **Isolation:** data from tenant A, or contact A, never appears for tenant B or contact B.
- [ ] **Audit and cost:** every side effect has an audit record; costs sum correctly by tenant and vendor.
- [ ] **Memory:** a fact from session 1 is recalled in session 2 for the same contact only.
- [ ] **Knowledge:** answers cite sources, and say "not found" when retrieval is below threshold.
- [ ] **Deploy:** one command deploys an instance to a clean AWS account, and rollback restores the previous config.

## Milestones and release plan

The template ships in five milestones, owned by Jag Pascoe with agent builders. There are no time goals: each milestone closes when its gate of acceptance tests passes. Client instances start once the modules they need have shipped.

| Milestone | Scope | Gate |
| --- | --- | --- |
| M0 Core | data model, event stream, agent loop, solution spec, tenant ids, offline tests | spec tests |
| M1 Agent core | two providers, tool registry, MCP and HTTP, permissions, budgets and secrets, TUI console, constructor v1 | conformance |
| M2 Runtime | headless service, channels and triggers, durable queue, approvals inbox, PII and consent, audit and Postgres, instance agent, basic AWS deploy, constructor v2 | governance |
| M3 Intelligence | 5-layer memory, knowledge (RAG), verification, agent teams, feedback loop, pack evals | eval suite |
| M4 Operations | Terraform (AWS), OpenTelemetry, cost reports, region policy, containers, control plane MVP, constructor v3; GCP and Azure later | v1.0 release |

Each milestone is tagged as a pre-release (0.x) in the repository once its gate passes; from M2 each tag also builds a container image. M4 ends with v1.0. Publishing to PyPI is deferred until outside adoption of the open core is wanted.

**First instantiation candidates (parked).** Chosen earlier for the first client instance, and kept on hold until M2 (runtime and governance) ships:

| Choice | Value |
| --- | --- |
| First pack | PyME Appointment Agent (clinics: confirm, remind, reschedule) |
| Channel | WhatsApp/SMS through a messaging gateway (Twilio-style) |
| Calendar | Google Calendar connector |
| Deployment | AWS, container + Terraform, single-tenant |
| Governance | Minimum set: PII tokens, consent, audit, retention |

## Risks, dependencies and open questions

The largest risk is building a general platform before any instance proves it; the gates and parked first instance keep scope honest.

**Risks**

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Template over-generalised before real instances | Months of platform work with no client value | Build modules only when a planned instance needs them; first instance starts right after M2 |
| Spec format too rigid or too loose | Instances fall back to custom code, eroding the 80% | Validate the spec against 3 different LOB shapes (PyME agent, Service Desk cell, RAG assistant) in M0 design review |
| Provider abstraction leaks vendor specifics | Model swaps need code changes | Conformance suite from M1; capability descriptor; evals decide swaps |
| Governance gaps with patient or financial data | Legal exposure under LFPDPPP and CNBV | Minimum governance is a gate before any client instance; legal review of the PII and consent design |
| Messaging channel constraints (WhatsApp templates, opt-in rules) | Delayed launches | Gateway adapter handles templates and opt-in; consent module enforces rules |
| Two runtimes in the company (OpenClaw/Paperclip internally, harness for clients) | Split know-how | Clear boundary: harness only for client solutions; shared conventions for skills and prompts |
| Small team maintaining a platform | Slow fixes, burnout | Boring defaults (Postgres queue, FastAPI), strict scope, agent-assisted development |

**Dependencies:** model APIs (Anthropic, OpenAI-compatible); `mcp` SDK; FastAPI; PostgreSQL and pgvector; Textual; a messaging gateway; AWS (ECS or EC2, RDS, Secrets Manager); Terraform; uv.

**Assumptions:** clients accept running solutions in their own cloud account; Di-Factory engineers (and agents) write specs; LFPDPPP is the first compliance target.

**Decisions from review (2026-09-27)**

| Question | Decision |
| --- | --- |
| Pack licensing | Open core: core and reference packs MIT; production packs are Di-Factory proprietary; each client fully owns its instance |
| Pack sequence | Appointment Agent first, then Service Desk cell, Conversational RAG assistant and the other PyME agents; Dev cell after v1 (needs the M4 container executor) |
| Messaging gateway account | Held by the client (zero markup, client owns numbers and templates); Di-Factory sets it up |
| Success targets | Accepted as listed under Success metrics |
| Naming and state | Distribution name `di-factory-general-harness` (not published to PyPI); state in `.dif/` and `~/.dif/`, project memory file `DIF.md` |
| Timeline | No time goals; milestones are ordered and gated by acceptance tests |
| Fleet operations | Control plane plus an outbound-only instance agent in each deployment; the client can revoke it (FR-27, FR-28) |
| Spec portability | Harness-native, kept clean so exporters to other platforms can come later |
| Delivery agents | Internal ones on OpenClaw/Paperclip; client-facing ones on the harness |
| Spec coverage | Paper tests passed on six shapes (three chat, batch, Dev cell, agent team) |
| Condition language | CEL subset |
| Language | English by default; Spanish only when a client asks |
| Eval format | YAML, one case per document |
| Constructor agent | Ships with the harness (`dif-general-harness build`) plus an OpenClaw skill for Teky; flow intake → match → interview → build → verify → approve → deploy → hand over |
| Deploy approval | Jag approves every deployment into a client cloud |
| Constructor timing | v1 in M1, v2 in M2, v3 in M4 |
| First-client deploy | Basic AWS deploy moves to M2; M4 hardens it |

No open questions remain; new ones will be added here.
