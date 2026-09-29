# Solution Spec v1 — draft

Status: **Draft for review** · 2026-09-27 · Implements `ARCHITECTURE.md` §3.0

A **solution spec** describes one Di-Factory client solution as data. The
harness loads it, validates it (Pydantic, in M0) and builds a tenant-scoped
**instance** from it. The goal is the PyME promise made concrete: the
**template** is the 80%, and the spec is the 20%.

This draft was checked on paper against six different solution shapes (§9):
- chat-style: a PyME Appointment Agent, a Service Desk cell and a
  Conversational RAG assistant;
- non-chat: a batch Receipt Processing job, a Dev cell and an OPC-style
  agent team.

## 1. Files and kinds

| Kind | What it is | Who writes it | Where it lives |
|---|---|---|---|
| `pack` | A reusable solution: agents, prompts, tools, workflows, policies, evals, and the **variables** a client must fill | Di-Factory (reference packs are MIT; production packs are proprietary) | `packs/<pack-id>/pack.json` + `prompts/`, `evals/` |
| `instance` | One client's deployment of one or more packs: variable values, connector secrets, overrides, tenant, deploy target | Di-Factory for the client; owned by the client | client repo or `instances/<tenant>/<id>.json` |

Both are JSON (decision 11). They carry `"$schema"` so editors can validate and
autocomplete, `"spec_version": "1"`, and `"kind": "pack" | "instance"`.

A pack directory:

```
packs/pyme-appointment-agent/
  pack.json          # the spec
  prompts/*.md       # system prompts and message templates in English (can use {{var.*}})
  evals/*.yaml       # scripted conversations with expected outcomes (YAML, one case per document)
  extensions/*.py    # optional escape hatch (see §8), counted against reuse
```

## 2. Top-level sections

| Section | Pack | Instance | Purpose |
|---|---|---|---|
| `solution` | ● | ● | id, version, name, LOB, description, locale |
| `extends` | ○ | ● | packs this spec builds on, with version ranges |
| `tenant` | – | ● | tenant id, display name, time zone |
| `variables` | ● | – | parameters the instance must or may set (the client's 20%) |
| `values` | – | ● | values for the pack's variables |
| `secrets` | ● | ○ | declared secret **names** and what they are for; never values |
| `models` | ● | ○ | per-role model config, provider settings, allowed regions |
| `agents` | ● | ○ | named agents: prompt, model role, tools, knowledge, memory, sub-agents, handoffs |
| `tools` | ● | ○ | tool packs, MCP servers, HTTP connectors, Python tools, per-tool effect overrides |
| `knowledge` | ○ | ○ | corpora, sources, chunking, retrieval settings, sync schedule |
| `memory` | ○ | ○ | which layers, scope (contact / instance), TTLs, promotion rules |
| `channels` | ○ | ○ | inbound/outbound channels, entry agent, message templates |
| `triggers` | ○ | ○ | schedules, relative timers, webhooks, events, batch runs |
| `workflows` | ○ | ○ | durable multi-step flows: agent steps, tool steps, approvals, waits, branches |
| `policies` | ● | ○ | guardrail profile, permissions, budgets, verification, escalation |
| `governance` | ● | ○ | PII classes, tokenization, consent, retention, audit, regions |
| `hitl` | ○ | ○ | approvals inbox, approvers, notification channels |
| `evals` | ● | ○ | eval suites and pass thresholds |
| `deploy` | – | ● | target (local, docker, aws), sizing, secret backend |
| `extensions` | ○ | ○ | Python modules for logic the spec cannot express (§8) |
| `workspaces` | ○ | ○ | sandboxed working copies (for example a git repo) and the executor they run in (§5.14) |
| `ledger` | ○ | ○ | the shared task ledger for agent teams: fields, backend, who can see it (§5.15) |

## 3. References and expressions

The spec stays declarative, with only these reference forms:

| Form | Example | Resolves to |
|---|---|---|
| Secret reference | `{"$secret": "twilio"}` | a value from the instance's secret backend at runtime; never stored, logged or shown to the model |
| Variable | `"{{var.clinic_name}}"` | the instance's value for a pack variable (strings, prompts, templates) |
| Contact / event field | `"{{contact.first_name}}"`, `"{{event.start}}"` | runtime data in templates, triggers and workflow inputs, **after** PII detokenization rules apply |
| Solution field | `"{{solution.locale}}"` | a value from the resolved `solution` section (for example the reply language) |
| File | `"prompts/receptionist.md"` | a file relative to the pack or instance directory |
| Tool name | `"calendar.find_slots"` | `<namespace>.<tool>` from the tool registry |
| Agent / workflow / channel id | `"receptionist"`, `"send-reminders"` | keys in the corresponding section |

**Conditions** (in workflows, escalation rules and triggers) use one small,
safe expression language: comparison, boolean logic, `in`, field access, and
no function calls. Example: `"ticket.priority in ['p1','p2'] and not contact.vip"`.
The language is a **CEL subset** (Common Expression Language: typed, safe,
with a Python implementation), decision 33.

## 4. Merge rules (pack → instance)

An instance lists packs in `extends`. The loader merges them left to right,
then applies the instance, following these rules:

1. **Objects deep-merge**; the later layer wins per key.
2. **Named maps merge by key.** `agents`, `tools.mcp`, `tools.http`,
   `channels`, `triggers`, `workflows` and `knowledge.corpora` all merge this
   way. `null` removes an entry.
3. **Arrays replace**, except where marked *additive* (below).
4. **Safety is monotonic.** A later layer can only tighten safety:
   - `policies.permissions.deny`, `governance.pii.classes` and
     `governance.consent` are **additive**;
   - retention can only shorten;
   - `governance.pii.tokenize` cannot be turned off if a pack turned it on;
   - budgets can be lowered in an instance, and raised only with an explicit
     `"override_reason"`.
5. **Variables must be satisfied.** Every `required` pack variable needs a
   value, and every declared secret needs a backend entry, or loading fails
   with a list of what is missing.

The merged result is the **resolved spec**. It is versioned (a hash plus a
semantic version), stored with every session and shown by
`dif-general-harness spec resolve`.

## 5. Section reference

### 5.1 `solution`, `tenant`, `variables`, `values`

```json
"solution": { "id": "pyme-appointment-agent", "version": "1.0.0", "name": "Appointment Agent",
              "lob": "pyme", "description": "...", "locale": "en" },
"tenant":   { "id": "clinica-sonrisa", "name": "Clínica Sonrisa", "timezone": "America/Mexico_City" },
"variables": {
  "business_name":  { "type": "string", "required": true, "description": "Shown to patients" },
  "reminder_hours": { "type": "integer", "default": 24, "min": 1, "max": 72 }
},
"values": { "business_name": "Clínica Sonrisa", "reminder_hours": 24 }
```

**Language (decision 34).** Packs are written in English and default to
`"locale": "en"`. When a client asks for Spanish, the instance sets
`"locale": "es-MX"`: agents reply in Spanish, and customer-facing templates are
overridden in the instance (`channels.<id>.templates.<name>.file`) with the
client's approved wording.

**Questionnaire (decision 36).** Each variable can carry an `ask` block. The
constructor agent uses it to interview whoever sets up a new client, so a new
pack brings its own questions:

```json
"reminder_hours": {
  "type": "integer", "default": 24, "min": 1, "max": 72,
  "ask": { "question": "How many hours before an appointment should patients be reminded?",
           "answered_by": "client", "example": "24", "group": "scheduling", "order": 3 }
}
```

- `answered_by` is `client`, `difactory` or `either`.
- `group` and `order` shape the interview.
- Secrets never get an `ask` for their value. The constructor only explains
  where the client must store them.

Variable types: `string`, `integer`, `number`, `boolean`, `enum`, `list`,
`object`, `duration` (`"24h"`), `schedule` (opening hours), `file`.

### 5.2 `secrets`

```json
"secrets": {
  "anthropic": { "description": "Model API key" },
  "twilio":    { "description": "Messaging gateway credentials (client-owned account)" }
}
```

The instance's `deploy.secrets_backend` decides where the values live: `env`,
`file` (dev only), `aws-secrets-manager`, `gcp-secret-manager` or
`1password`.

### 5.3 `models`

```json
"models": {
  "roles": {
    "main":       { "provider": "anthropic", "model": "{{var.main_model}}", "effort": "medium" },
    "router":     { "provider": "anthropic", "model": "{{var.fast_model}}", "effort": "low" },
    "verifier":   { "provider": "openai-compatible", "model": "...", "effort": "high" }
  },
  "providers": {
    "anthropic":         { "api_key": { "$secret": "anthropic" }, "via": "direct" },
    "openai-compatible": { "base_url": "{{var.llm_base_url}}", "api_key": { "$secret": "llm" } }
  },
  "allowed_regions": ["us", "mx"]
}
```

The roles are `main`, `subagent`, `verifier`, `compaction`,
`memory_extraction`, `router` and `title`. Agents pick a role; they never
name a model directly. A model swap is therefore one edit, and the pack's
evals decide whether it passes.

### 5.4 `agents`

```json
"agents": {
  "receptionist": {
    "description": "Confirms, reminds and reschedules appointments",
    "prompt": "prompts/receptionist.md",
    "model_role": "main",
    "tools": ["calendar.*", "messaging.send", "contacts.lookup"],
    "knowledge": ["clinic-faq"],
    "memory": { "scope": "contact" },
    "subagents": [],
    "handoffs": ["human"],
    "max_turns": 12
  }
}
```

- `tools` accepts globs over registry names.
- `handoffs` lists agents (or the reserved `human`) this agent may transfer
  a conversation to.
- `subagents` are called as tools and return only their final answer.
- A **team** is simply several agents with handoffs, plus a shared ledger when
  `workflows` or `memory.shared_ledger` is enabled.

### 5.5 `tools`

```json
"tools": {
  "packs": ["general", "connectors/google-calendar"],
  "mcp":   { "helpdesk": { "transport": "http", "url": "{{var.helpdesk_mcp_url}}", "auth": { "$secret": "helpdesk" } } },
  "http":  {
    "identity": {
      "base_url": "{{var.idp_url}}", "auth": { "type": "bearer", "token": { "$secret": "idp" } },
      "operations": {
        "reset_password": { "method": "POST", "path": "/users/{user_id}/reset", "effect": "external",
                            "input": { "user_id": "string" }, "verify": "identity-verified" }
      }
    }
  },
  "python": ["extensions.slots:find_best_slot"],
  "config":  { "connectors/google-calendar": { "credentials": { "$secret": "google" }, "calendar_ids": "{{var.calendar_ids}}" } },
  "overrides": { "calendar.delete_event": { "permission": "deny" } }
}
```

- Every tool resolves to a JSON-schema contract with an `effect`: `read`,
  `write` (local state) or `external` (changes something outside the
  harness).
- `verify` names a check from `policies.verification.checks` that must pass
  before the call commits.
- `config` passes settings and secrets to a tool pack.

### 5.6 `knowledge`

```json
"knowledge": {
  "corpora": {
    "clinic-faq": {
      "sources": [ { "type": "file", "path": "knowledge/faq.md" },
                   { "type": "gdrive", "folder_id": "{{var.faq_folder}}", "auth": { "$secret": "gdrive" } } ],
      "chunking": "layout",
      "retrieval": { "mode": "hybrid", "top_k": 6, "min_score": 0.35, "cite": true, "not_found": "say_so" },
      "sync": { "schedule": "0 */6 * * *" }
    }
  }
}
```

Agents reach a corpus through the generated `knowledge.search_<corpus>` tool.

- An agent gets the search tool of each corpus in its `knowledge` list (or matching its
  `tools` globs).
- `min_score` (0 to 1) is the share of the query's information a passage must cover;
  nothing above it means "not found", and `not_found` says what the agent does then
  (`say_so` or `handoff`). Escalation rules see `knowledge.not_found`, `knowledge.corpus`
  and `knowledge.query`.
- With `cite: true`, passages carry `kb:<id>` markers; a `citations` check
  (`min_citations`, `claims_must_cite`) runs on answers that used them. A failing answer
  gets one rewrite, then "not found". Contacts see `[1]` and a Sources list.
- `file` sources (a file or a folder) are synced when the instance opens and on
  `sync.schedule`; `on_delete: propagate` (the default) removes files that disappeared.
  Markdown, text, HTML and CSV are read; other formats are reported. Other sources push
  documents with `PUT /admin/knowledge/{corpus}/documents`.

### 5.7 `memory`

```json
"memory": {
  "layers": ["episodic", "semantic", "procedural"],
  "scope": "contact",
  "episodic_ttl": "90d",
  "skill_promotion": { "min_successes": 3, "approval": "required" },
  "shared_ledger": false
}
```

Working memory and forgetting are always on. The scope is `contact` (per
end user), `instance` or `agent`. It is always tenant-scoped underneath.

### 5.8 `channels`

```json
"channels": {
  "whatsapp": {
    "type": "gateway", "provider": "twilio", "credentials": { "$secret": "twilio" },
    "address": "{{var.whatsapp_number}}", "entry_agent": "receptionist",
    "contact_key": "phone",
    "templates": { "reminder": { "file": "prompts/tpl_reminder.md", "provider_template_id": "{{var.tpl_reminder_id}}" } },
    "session_window": "24h"
  }
}
```

Channel types: `gateway` (WhatsApp/SMS via Twilio-style providers),
`telegram`, `web`, `email`, `slack`, `api`, and later `voice`. Outbound
messages outside a provider's session window must use a template.

### 5.9 `triggers`

```json
"triggers": {
  "daily-sync":     { "type": "schedule", "cron": "0 7 * * *", "workflow": "plan-reminders" },
  "reminder":       { "type": "relative", "source": "calendar.events", "offset": "-{{var.reminder_hours}}h",
                      "workflow": "send-reminder", "requires_consent": true },
  "ticket-created": { "type": "webhook", "path": "/hooks/helpdesk", "auth": { "$secret": "helpdesk_webhook" },
                      "workflow": "resolve-ticket" },
  "sla-breach":     { "type": "delay", "after": "4h", "unless": "ticket.status == 'solved'", "workflow": "escalate" }
}
```

| Type | What starts it |
|---|---|
| `schedule` | cron, in the tenant's time zone |
| `relative` | a time offset from a data field (for example 24 h before an appointment) |
| `delay` | a timer started by a workflow step, which can be cancelled |
| `webhook` | a client system calls the harness |
| `event` | an internal event (for example `escalation.resolved`) |
| `batch` | runs over a list of items |

`requires_consent` makes the consent module skip contacts who opted out.

A trigger that targets an agent can deliver the agent's reply: `channel` names the channel
and `to` the address (default: the channel's `address`), for example a founder's daily
brief. Delivery to a `contact` channel goes through consent; operator channels (`hitl`,
`founder`, `outbound`) are exempt. The channel must exist, and it needs an address or `to`.

### 5.10 `workflows`

```json
"workflows": {
  "send-reminder": {
    "input": { "event": "calendar.event" },
    "steps": [
      { "id": "msg",     "type": "template", "channel": "whatsapp", "template": "reminder", "to": "{{event.contact}}" },
      { "id": "wait",    "type": "wait", "for": "reply", "timeout": "12h" },
      { "id": "handle",  "type": "agent", "agent": "receptionist", "input": "{{steps.wait.reply}}", "when": "steps.wait.replied" },
      { "id": "nudge",   "type": "template", "channel": "whatsapp", "template": "reminder_nudge", "when": "not steps.wait.replied" }
    ],
    "on_error": "escalate"
  }
}
```

Step fields (every step has `id` and `type`, and may have `when`: a condition; when it
does not hold, the step is skipped):

| Type | Fields | Output (`steps.<id>`) |
|---|---|---|
| `agent` | `agent`, `input` (text or object) | `text`, `reason`, `session`, and the fields of a JSON answer |
| `tool` | `tool`, `args` | the tool's result; an `ask` tool waits for approval first |
| `template` | `channel`, `template`, `to` (default: the channel's `address`), `vars` | `sent`, `to`, `channel` |
| `message` | `channel`, `text` (or `body`), `to` | `sent`, `to`, `channel` |
| `approval` | `summary`, `on_reject` (a step id, `end` or `escalate`) | `approved`, `by`, `note` |
| `wait` | `for`: `reply`, `event` (`event`, `match`) or `time` (`duration`); `timeout` | `replied`, `reply`, the event, or `timed_out` |
| `branch` | `cases`: `[{when, goto}]` (the first that holds) | `goto` |
| `parallel` | `branches`: agent and tool steps, run together | one output per branch |
| `handoff` | `to` (`human` or an agent), `reason`, `input`, `outcome` | `escalation`, or the agent's output |
| `timer` | `trigger` (a `delay` trigger to arm) | the armed job |
| `end` | `outcome` | `outcome` |

Workflow fields: `input`, `steps`, `concurrency` (runs at once), `on_error` (`escalate`
files an inbox item). Templates in step fields read `input`, `steps`, `var`, `event` and
`contact`; a field that is exactly one `{{ }}` keeps the value's type.

Every step is persisted, so a restart resumes at the last completed step. A step that
crashed half way runs again on resume, so side-effecting tools should be idempotent.

### 5.11 `policies`

```json
"policies": {
  "profile": "strict",
  "permissions": {
    "allow": ["calendar.find_slots", "calendar.get_event", "knowledge.*"],
    "ask":   ["calendar.move_event"],
    "deny":  ["calendar.delete_event"]
  },
  "budgets": { "per_run": { "usd": 0.20, "turns": 12 }, "per_tenant_day": { "usd": 5 } },
  "verification": {
    "checks": {
      "no-double-booking": { "type": "tool", "tool": "calendar.check_conflicts", "expect": "no_conflicts" },
      "identity-verified": { "type": "condition", "expr": "contact.verified == true" }
    },
    "verifier": { "model_role": "verifier", "applies_to": ["effect:external"], "mode": "critical_only" }
  },
  "escalation": {
    "rules": [
      { "when": "intent in ['medical_advice','complaint']", "to": "human" },
      { "when": "verification.failed_twice", "to": "human" }
    ],
    "handoff_to": { "type": "inbox" }
  }
}
```

- Check types:
  - `tool`: call a read tool and compare the result;
  - `condition`: an expression over context;
  - `citations`: the answer cites retrieved sources;
  - `verifier`: the verifier agent judges against listed criteria.
- `policies.router` lists intents answered by the cheap `router` role without
  running the main agent.
- Permission rules match tool names, argument patterns (`"messaging.send(to=+52*)"`)
  or effects (`"effect:external"`).
- Profiles set the defaults; explicit rules win.
- Headless `ask` rules go to the approvals inbox.

### 5.12 `governance`

```json
"governance": {
  "pii": { "classes": ["name", "phone", "email", "curp", "rfc", "health"], "tokenize": true,
           "reveal_in_output": ["name"] },
  "consent": { "required": true, "channels": ["whatsapp"], "opt_out_keywords": ["BAJA", "STOP"] },
  "retention": { "conversations": "180d", "episodic": "90d", "audit": "5y" },
  "audit": { "level": "full" },
  "regions": { "data": "mx", "models": ["us", "mx"] },
  "compliance": ["lfpdppp"]
}
```

### 5.13 `hitl`, `evals`, `deploy`

```json
"hitl":   { "approvers": ["role:front_desk"], "notify": [{ "channel": "email", "to": "{{var.ops_email}}" }],
            "approval_timeout": "2h", "on_timeout": "reject" },
"evals":  { "suites": ["evals/booking.yaml", "evals/reschedule.yaml", "evals/pii.yaml"],
            "thresholds": { "pass_rate": 0.9, "unsafe_actions": 0 } },
"deploy": { "target": "aws", "profile": "small", "region": "us-east-1",
            "secrets_backend": "aws-secrets-manager", "database": "postgres" }
```

### 5.14 `workspaces` (added by paper test 2)

```json
"workspaces": {
  "repo": {
    "type": "git", "source": "https://github.com/{{var.github_org}}/{{input.repo}}",
    "credentials": { "$secret": "github_app" },
    "executor": { "type": "container", "image": "{{var.sandbox_image}}", "network": "deny-by-default",
                  "allow_hosts": ["pypi.org"], "cpu": 2, "memory": "4g", "timeout": "30m" }
  }
}
```

An agent with `"workspace": "repo"` gets its coding tools bound to that
sandbox. `command` checks run in the same workspace.

### 5.15 `ledger` (added by paper test 2)

```json
"ledger": {
  "backend": "builtin",
  "fields": { "title": "string", "owner": "agent", "status": "enum:todo,doing,blocked,done",
              "due": "date", "needs_founder": "boolean" },
  "visible_to": ["cgo", "coo", "cto", "human"]
}
```

The ledger is exposed through the built-in `ledger.*` tools. Assigning a task
to an agent emits `ledger.task_assigned`, which a trigger can route to that
agent. The backend is `builtin` (Postgres); external boards (Linear, Notion)
come later as adapters.

### 5.16 Other additions from paper test 2

| Where | Addition |
|---|---|
| `agents.*` | `output_schema` (a JSON Schema file for structured results), `workspace`, and per-agent `budgets` |
| `tools` | **Built-in namespaces:** `ledger`, `runs`, `knowledge`, `memory`. **Tool packs declare their namespaces** (`general` → `http`, `web`, `notes`), so specs reference tools, not packs. New `documents` pack (read, OCR). |
| `triggers` | type `file` (new files in a bucket or folder, with `match` and a required `dedupe_key`); an optional `when` CEL filter; a trigger can target an **agent** (`agent` + `input`) instead of a workflow, including `{{event.*}}` routing |
| `workflows` | `concurrency`; step types `message` (free text to a channel), `timer` (start a `delay` trigger) and `parallel` (with `branches`); `approval` gets `summary` and `on_reject`; `end` gets an `outcome` |
| `policies.verification.checks` | type `command` (run a command in a workspace; for example, tests must exit 0) |
| `governance.pii` | `reveal_to_tools`: which PII classes are de-tokenized for which tools (for example, the RFC must reach the ERP) |
| `governance.consent` | applies to messages sent **to contacts**; channels with purpose `hitl`, `founder` or `outbound` (operators) are exempt |

Eval suites are YAML, one case per document; every case runs in a fresh instance under
the headless service (channels record, fixtures replace connectors, approvals are never
granted):

```yaml
id: confirm-1
setup:
  contact: { first_name: Ana, consent: true }      # key: optional
  event: { id: evt-1, date: "2026-10-05", time: "10:00" }   # for relative triggers
  fixtures: { calendar.get_event: { id: evt-1, status: confirmed } }
turns:
  - trigger: reminder                  # or user: "text", or advance: 12h
  - expect_template: reminder          # also expect_message, expect_no_message
  - user: "1"
  - expect_tool: calendar.get_event
  - expect_reply_contains: [confirm]   # also expect_handoff (human | agent), expect_approval
must_not:
  - tool: calendar.move_event          # never even attempted
  - reply_category: medical_advice     # judged by the verifier role
```

An outcome case uses `setup.message` and `expect` (`tools_called`, `must_not`,
`approval_requested`, `handoff`, `template`). A case that asks for something the runner
cannot check is skipped with the reason, never passed. Results are stored per config
version, and cases that passed in the previous run and fail now are reported as
regressions.

## 6. Validation (what the loader enforces)

1. JSON Schema and Pydantic types for every section.
2. Every reference resolves: agents, tools (after registry load), channels,
   workflows, templates, corpora, checks, secrets, variables and files.
3. Every agent's tools are covered by a permission rule or profile default.
   A tool with an `external` effect needs an `ask`, a `verify` or an explicit
   `allow` with an `override_reason`.
4. Governance: if any channel carries PII classes, `pii.tokenize` must be
   true unless `override_reason` is given; consent is required for triggered
   outbound messaging.
5. The monotonic safety rules (§4) hold after merging.
6. The deploy region satisfies `governance.regions.data` (for example `mx` →
   AWS `mx-central-1`).
7. Every pack declares at least one eval suite.
8. Agent tool wildcards (`coding.*`, `crm.*`) are **expanded against the tool
   registry at load time**. Every resulting tool with an `external` effect must
   be covered by `ask`, `verify`, or an explicit `allow` with a reason.
9. References to `var.*` inside CEL conditions count as uses of the variable.

## 7. Versioning

- Packs use semver.
- Instances pin ranges (`"pyme-appointment-agent@^1.2"`).
- A new resolved spec is a **config version**. It can be rolled back and is
  hot-reloaded when it passes validation and the pack evals.

## 8. Escape hatch: `extensions`

Some client logic will not fit the spec, for example a clinic-specific slot
ranking. `tools.python` registers Python tools (`"extensions.slots:best_slot"` is
`extensions/slots.py` next to the declaring spec, and the tool is `slots.best_slot`),
versioned with the pack or instance and covered by the deploy signature. A plain typed
function becomes a `read` tool; `@tool(..., effect=...)` declares another effect. The list
accumulates across layers. They run under the same permissions, budgets and audit as
everything else. Extension lines count against the **reuse ratio** metric
(target ≥ 80%). A pack that repeatedly needs the same extension should absorb
it as a variable or tool.

## 9. Paper test: three solution shapes

Full examples are in [`examples/`](examples/):

| Shape | Files | What it exercises |
|---|---|---|
| PyME Appointment Agent | [`pyme-appointment-agent/pack.json`](examples/pyme-appointment-agent/pack.json) + [`instances/clinica-sonrisa.json`](examples/instances/clinica-sonrisa.json) | gateway channel with templates, relative triggers, consent, calendar connector, verification before booking, variables |
| Service Desk cell | [`service-desk-cell/pack.json`](examples/service-desk-cell/pack.json) | agent team (triage, resolver, knowledge sub-agent), webhook and SLA triggers, MCP helpdesk, HTTP identity tool with verification, approvals, escalation |
| Conversational RAG assistant | [`conversational-rag/pack.json`](examples/conversational-rag/pack.json) | knowledge corpora with sync, citations and "not found", multi-channel, intent router short-circuit, no side-effect tools |

### What the paper test changed

Writing the three examples exposed gaps in the first draft. Each was fixed in
the spec above:

| Gap found | Found in | Fix |
|---|---|---|
| Packs need client-specific parameters that aren't secrets or prompts (business name, hours, reminder offset) | Appointment | `variables` in packs, `values` in instances, `{{var.*}}` interpolation |
| "Remind 24 h before each appointment" cannot be a cron | Appointment | `relative` trigger type (offset from a data field) |
| WhatsApp messages outside the 24 h window need approved templates | Appointment | channel `templates`, a `session_window`, and a `template` workflow step |
| Waiting for a patient's reply is not an agent turn | Appointment | `wait` step with `for: reply` and a timeout |
| SLA timers must be cancellable when the ticket is solved | Service Desk | `delay` trigger with `unless` |
| A password reset must only happen after identity is verified | Service Desk | tool-level `verify` pointing to a named check |
| Several agents in one solution need an entry point and rules for passing work | Service Desk | channel `entry_agent`, agent `handoffs`, reserved `human` |
| A knowledge corpus must stay current | RAG | `knowledge.corpora.*.sync` schedule |
| Cheap answers to trivial questions without the main model | RAG | `router` model role and an intent short-circuit in `policies` |
| An instance could weaken a pack's safety by overriding it | All | monotonic merge rules (§4) and validation rule 5 (§6) |
| Tool packs need their own settings and credentials | Appointment | `tools.config` per pack |
| `helpdesk.solve_ticket` changed client state with only a sampled verifier guarding it | Service Desk (checker) | `verifier` check type; the tool now has `verify: resolution-verified` and an explicit allow |
| The clinic instance deployed to `us-east-1` while its governance requires data in Mexico | Instance (checker) | validation rule 6: the deploy region must satisfy `governance.regions.data` |

The references were checked by a throwaway script, which will become the M0
loader's validation tests: variables, secrets, model roles, agents, corpora,
tool namespaces, workflows, templates, checks, permission coverage of external
tools, consent for outbound triggers, required instance values and monotonic
retention. The last run passed with no problems. Only one eval file
(`pyme-appointment-agent/evals/confirm.yaml`) is written, to fix the eval
format; the rest are listed but not written yet.

### Paper test 2: non-chat shapes (decision 32)

| Shape | File | What it exercises |
|---|---|---|
| Batch document job | [`pyme-receipt-processing/pack.json`](examples/pyme-receipt-processing/pack.json) | file trigger with dedupe, structured extraction to a JSON Schema, validation, duplicate check, amount-based approval, posting to an ERP, daily report, PII revealed only to the ERP |
| Dev cell | [`dev-cell/pack.json`](examples/dev-cell/pack.json) | GitHub webhook with a CEL filter, sandboxed git workspace in a container, coding tools, tests as a `command` check before opening a PR, merges denied |
| OPC-style agent team | [`opc-c-suite/pack.json`](examples/opc-c-suite/pack.json) | long-lived CGO/COO/CTO agents, shared ledger, schedules that wake agents directly, event-routed delegation, parallel stand-up, founder approvals over Telegram |

**What paper test 2 changed:**

| Gap found | Found in | Fix |
|---|---|---|
| Work arrives as files, not messages | Receipts | `file` trigger with `match` and a required `dedupe_key` |
| An extraction must return exact fields | Receipts | agent `output_schema` (JSON Schema) |
| The RFC is tokenized for the model, but the ERP needs the real value | Receipts | `governance.pii.reveal_to_tools` |
| The daily report to staff was flagged as needing consent | Receipts | consent covers messages to contacts only; operator channels are exempt |
| Summaries and ledgers need tools no pack provides | Receipts, OPC | built-in namespaces (`runs`, `ledger`, `knowledge`, `memory`) |
| Pack names ≠ tool names (`general` gives `http.get`) | OPC | packs declare their namespaces |
| Coding agents need an isolated working copy and toolchain | Dev cell | `workspaces` with a container executor |
| "Tests pass" is a command, not a tool call or condition | Dev cell | `command` check type |
| Only issues with a given label should start work | Dev cell | CEL `when` filter on triggers |
| `coding.*` and `crm.*` grant tools the spec never names | Dev cell, OPC | wildcards expanded at load time (validation rule 8) |
| Long-lived agents must wake up on their own schedules | OPC | triggers that target an `agent` directly |
| Agents delegate work to each other | OPC | `ledger` section and a `ledger.task_assigned` event |
| Each executive needs its own spending cap | OPC | per-agent `budgets` |
| I first modelled "drafts" as a fake HTTP call | OPC (self-review) | removed; drafts are ledger items |

**Roadmap consequences:**
- The **Dev cell needs the container executor**, which stays in M4 (decision
  40). The Dev cell pack ships after v1.
- The **`documents` tool pack** (OCR, parsing) and the **built-in ledger** are
  new modules. The ledger fits M3 (agent teams); `documents` is needed when
  Receipt Processing is first sold.

The checker ran on all six packs plus the instance and reported no problems.
It was then mutation-tested with planted errors (unknown workspace, unknown
agent, missing ledger, missing `dedupe_key`), and it caught all four.

## 10. Resolved questions

| Question | Decision |
|---|---|
| Condition language | CEL subset (decision 33) |
| Prompt language | English by default; Spanish only when a client asks; instances override customer-facing templates (decision 34) |
| Eval file format | YAML, one case per document (decision 35) |

Paper test 2 is done (see above).
