# CLAUDE.md — dif-general-harness

Di-Factory's general solution template: an agent runtime where every client
solution is a declarative **solution spec** (a pack plus a client instance).
Read `docs/ARCHITECTURE.md` (40 decisions, §7) before changing behaviour;
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
uv run dif-general-harness run|console|eval INSTANCE.json --packs DIR
```

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
- `src/dif_general_harness/store/`: append-only JSONL session store (redacted)
- `src/dif_general_harness/runtime/`: `Instance` (spec to runnable agents), role routing, prompts
- `src/dif_general_harness/console/`: Textual TUI
- `src/dif_general_harness/constructor/`: catalog matching, interview, build, evals
- `docs/spec/examples/`: six example packs + one instance, which are also the **test fixtures**
- `tests/test_acceptance_m1.py`: the M1 gate (spec, model swap, tool contract, permissions)

## Rules that must not be broken

- **Own the loop.** No agent frameworks (LangGraph and similar). Provider SDK types never
  leave `providers/`; the core only sees the neutral message and event model.
- **Tenant scope everywhere.** Every event and stored record carries a `Scope`
  (`tenant_id`, `instance_id`). Storage paths are namespaced by it.
- **Tools never raise into the loop.** Every call ends in `ok | denied | error | timeout`.
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

M1 (agent core) is done: its gate is `tests/test_acceptance_m1.py`. Next: M2 headless runtime
and governance minimum (FastAPI service, channels and triggers, durable queue, Postgres,
approvals inbox, PII/consent/audit, instance agent, basic AWS deploy, constructor v2).
Known M1 gaps that M2/M3 close: verification checks (tools with `verify` are forced to
`ask`), knowledge and ledger tools, the calendar connector, Python tool loading, and eval
steps that need triggers, templates or handoffs (reported as skipped). See
`docs/ARCHITECTURE.md` §6.
