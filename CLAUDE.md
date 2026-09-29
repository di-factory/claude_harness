# CLAUDE.md — dif-general-harness

Di-Factory's general solution template: an agent runtime where every client
solution is a declarative **solution spec** (a pack plus a client instance).
Read `docs/ARCHITECTURE.md` (42 decisions, §7) before changing behaviour;
`docs/spec/SOLUTION_SPEC.md` is the spec contract.

## Commands

```bash
uv sync                                   # Python 3.12, deps + dev tools
uv run pytest -q                          # all tests, offline (FakeProvider, no API keys)
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy                               # strict
uv run dif-general-harness spec validate docs/spec/examples/dev-cell
uv run dif-general-harness spec resolve docs/spec/examples/instances/clinica-sonrisa.json
uv run dif-general-harness build --request "..." [--answers FILE] --packs docs/spec/examples
uv run dif-general-harness run|console|eval|serve INSTANCE.json --packs DIR
uv run dif-general-harness keys new jag | approve ... | deploy ... --target docker|aws
```

Tests start a throwaway local Postgres (unix socket, `tests/conftest.py`) and run the
storage tests on SQLite and Postgres; they skip Postgres when it is not installed.

All four checks must pass before every commit.

## Layout

- `src/dif_general_harness/core/`: scope (tenant ids), messages, events, session, agent loop
  (`ToolGate` and `Meter` protocols keep policy out of the loop)
- `src/dif_general_harness/spec/`: schema (Pydantic), loader (catalog, merge, interpolation), validate
- `src/dif_general_harness/providers/`: provider protocol, Anthropic, OpenAI-compatible, `FakeProvider`
- `src/dif_general_harness/tools/`: registry (`@tool`, input checks), HTTP connectors, MCP client,
  `packs/` (coding, general)
- `src/dif_general_harness/policy/`: permissions and approvals, budgets, secret redaction
- `src/dif_general_harness/tenancy/`: secret backends and `$secret` resolution
- `src/dif_general_harness/store/`: database layer (SQLite/Postgres, migrations), SQL and JSONL
  session stores (redacted)
- `src/dif_general_harness/workflows/`: durable job queue and worker (the engine is M3)
- `src/dif_general_harness/governance/`: PII tokenization, consent, audit chain, retention
- `src/dif_general_harness/runtime/`: `Instance` (spec to runnable agents), role routing, prompts
- `src/dif_general_harness/channels/`, `triggers/`, `hitl/`: adapters, cron, the inbox
- `src/dif_general_harness/service/`: the headless runtime (`Headless`), FastAPI app, config boot
- `src/dif_general_harness/tenancy/`: secrets (env, file, AWS) and config versions
- `src/dif_general_harness/fleet/`: the outbound-only instance agent
- `src/dif_general_harness/console/`: Textual TUI
- `src/dif_general_harness/constructor/`: matching, interview, build, evals, approve and deploy
- `deploy/docker/`, `deploy/terraform/aws/`: the image and the basic AWS module
- `integrations/openclaw-skill/`: the wrapper Teky uses to drive the constructor
- `docs/spec/examples/`: six example packs + one instance, which are also the **test fixtures**
- `tests/test_acceptance_m1.py`, `tests/test_acceptance_m2.py`: the milestone gates;
  `tests/support.py` holds the service test fixtures

## Rules that must not be broken

- **Own the loop.** No agent frameworks (LangGraph and similar). Provider SDK types never
  leave `providers/`; the core only sees the neutral message and event model.
- **Tenant scope everywhere.** Every event and stored record carries a `Scope`
  (`tenant_id`, `instance_id`). Storage paths are namespaced by it.
- **Tools never raise into the loop.** Every call ends in `ok | denied | error | timeout`.
- **Jobs belong to their instance.** Queues are scoped: an instance never claims another's
  jobs, even in a shared database.
- **Deploys need Jag's signature** over the exact staged solution; never weaken
  `constructor/deploy.py` checks.
- **Tests are offline.** No network, no API keys: `FakeProvider`, `httpx2.MockTransport`,
  in-process MCP servers.
- **Safety is monotonic.** A later spec layer can only tighten governance, consent,
  retention, deny rules and budgets (see `spec/loader.py`). Never weaken these rules.
- **Every validation rule has a planted-error test** in `tests/test_spec_rules.py`.
  Add one whenever you add a rule.
- **Specs are JSON; evals are YAML; prompts are English by default** (Spanish only when a
  client asks).
- The harness is one of several base platforms. It does **not** run Di-Factory itself
  (OpenClaw/Paperclip do); only client solutions and client-facing agents run here.
- No time goals: milestones are done when their acceptance gate passes.

## Current milestone

M2 (headless runtime and governance minimum) is done: its gate is
`tests/test_acceptance_m2.py`. Next: M3 intelligence modules (5-layer memory, knowledge/RAG,
verification, workflows and agent teams, feedback loop, evals per solution).
Known gaps M3 closes: verification checks (tools with `verify` are forced to `ask`), the
workflow engine and CEL conditions (workflow and conditional triggers are reported, not
run), knowledge and ledger tools, the calendar connector, Python tool loading, email and
Slack channels, and eval steps that need triggers, templates or handoffs. See
`docs/ARCHITECTURE.md` §6.
