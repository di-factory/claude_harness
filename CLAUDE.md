# CLAUDE.md — dif-general-harness

Di-Factory's general solution template: an agent runtime where every client
solution is a declarative **solution spec** (a pack plus a client instance).
Read `docs/ARCHITECTURE.md` (81 decisions, §7) before changing behaviour;
`docs/spec/SOLUTION_SPEC.md` is the spec contract.

## Commands

```bash
uv sync                                   # Python 3.12, deps + dev tools
uv run pytest -q                          # all tests, offline (FakeProvider, no API keys)
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy                               # strict
uv run dif-general-harness spec validate docs/spec/examples/dev-cell
uv run dif-general-harness spec resolve docs/spec/examples/instances/clinica-sonrisa.json
uv run dif-general-harness spec copy INSTANCE.json clients/NAME [--id ID]   # with its files
./setup.sh                                # fresh server: install, guided setup, optionally online
uv run dif-general-harness setup [--public-url URL]          # the guided setup on its own
uv run dif-general-harness questionnaire --pack ID --for client|difactory --out FILE  # onboarding
uv run dif-general-harness build --request "..." [--answers FILE ...] --packs docs/spec/examples
uv run dif-general-harness secrets set NAME | secrets check INSTANCE.json   # ~/.dif/secrets
uv run dif-general-harness run|console|serve INSTANCE.json --packs DIR
uv run dif-general-harness eval INSTANCE.json --packs DIR   # fresh instance per case; drift
uv run dif-general-harness replay INSTANCE.json --packs DIR # real conversations, again, judged
uv run dif-general-harness keys new jag | approve ... | deploy ... --target docker|aws
uv run dif-general-harness adjust|upgrade INSTANCE.json ... --dry-run     # constructor v3
uv run dif-general-harness costs INSTANCE.json --by vendor,model         # spend + quality
uv run dif-general-harness admin status|inbox|show|reply|faq|costs         # a running instance
uv run dif-general-harness handover INSTANCE.json --owner NAME --lang es  # the client's Claude
                                          # the whole handover, guided: /handover (.claude/skills)
uv run dif-general-harness fleet register|offer|rollout|rollback|status  # via the control plane
uv run dif-general-harness control serve --key KEY                       # the control plane
terraform -chdir=deploy/terraform/aws init -backend=false && terraform -chdir=deploy/terraform/aws test
```

Tests start a throwaway local Postgres (unix socket, `tests/conftest.py`) and run the
storage tests on SQLite and Postgres; they skip Postgres when it is not installed.

All four checks must pass before every commit.

Setting up a server or a first client: `docs/GETTING_STARTED.md` (phases, checkpoints and the
setup mistakes the harness now catches early). Keep it in step with the CLI.

## Layout

- `src/dif_general_harness/core/`: scope (tenant ids), messages, events, session, agent loop
  (`ToolGate` and `Meter` protocols keep policy out of the loop), `cel.py` (conditions)
- `src/dif_general_harness/spec/`: schema (Pydantic), loader (catalog, merge, interpolation), validate
- `src/dif_general_harness/providers/`: provider protocol, Anthropic, OpenAI-compatible, `FakeProvider`
- `src/dif_general_harness/tools/`: registry (`@tool`, input checks), HTTP connectors, MCP client,
  `python.py` and `python_sandbox.py` (pack extensions), `egress.py` (the sandbox proxy),
  `packs/` (coding, general, documents, google_calendar)
- `src/dif_general_harness/policy/`: permissions and approvals, budgets, secret redaction
- `src/dif_general_harness/tenancy/`: secret backends and `$secret` resolution
- `src/dif_general_harness/store/`: database layer (SQLite/Postgres, migrations), SQL and JSONL
  session stores (redacted)
- `src/dif_general_harness/workflows/`: durable job queue and worker, workflow engine, `render`
- `src/dif_general_harness/verify/`: verification checks (tool, condition, command, verifier)
- `src/dif_general_harness/teams/`: ledger, sub-agent tools, `handoff.agent`, `runs.*`
- `src/dif_general_harness/memory/`: scoped episodic/semantic/procedural store, `memory.*` tools
- `src/dif_general_harness/knowledge/`: chunking, sync, retrieval, citations, `knowledge.search_*`
- `src/dif_general_harness/documents/`: text from PDF/DOCX/XLSX, OCR with a vision role
- `src/dif_general_harness/feedback/`: candidate and pinned constraints
- `src/dif_general_harness/governance/`: PII tokenization, consent, audit chain, retention
- `src/dif_general_harness/runtime/`: `Instance` (spec to runnable agents), role routing, prompts, skills
- `src/dif_general_harness/channels/`, `triggers/`, `hitl/`: adapters (gateway, Telegram,
  API/web, email, Slack, voice), cron, file sources, the inbox
- `src/dif_general_harness/service/`: the headless runtime (`Headless`), FastAPI app, config boot,
  `hooks.py` (signed events to client systems)
- `src/dif_general_harness/tenancy/`: secrets (env, file, AWS) and config versions
- `src/dif_general_harness/fleet/`: the outbound-only instance agent (signed, eval-gated offers)
- `src/dif_general_harness/control/`: the control plane (fleet view, offers, rollouts, audit)
- `src/dif_general_harness/observability/`: cost ledger and reports, quality metrics, OTLP traces
- `src/dif_general_harness/console/`: Textual TUI
- `src/dif_general_harness/constructor/`: matching, interview, build, evals, approve and deploy
- `deploy/docker/`, `deploy/terraform/aws/`: the image and the AWS module (profiles, alarms,
  `tests/*.tftest.hcl` against a mocked provider)
- `integrations/openclaw-skill/`: the wrapper Teky uses to drive the constructor
- `docs/spec/examples/`: six example packs + one instance, which are also the **test fixtures**
- `tests/test_acceptance_m1.py` … `tests/test_acceptance_m4.py`: the milestone gates;
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

v1.0: M0 to M4 are done; the last gate is `tests/test_acceptance_m4.py` (one-command deploy
plan and rollback, costs by tenant and vendor, eval-gated fleet rollouts, regions and the
sandbox). After v1.0 the listed gaps were closed (decisions 58–64): context compaction and
the intent router, file and batch triggers, the documents pack, sampled verification, the
egress proxy and isolated extensions, hybrid retrieval with S3/Drive/web sources, and voice;
then the web chat page and the setup's advisers (pack advisor, business consultant; 65–66) reading small FAQs whole (67), and
the client's look from any brand material plus missing-key explanations (68), and the
handover to a client's own Claude with the `admin` commands and owner FAQ edits (69); then
untrusted-content fences, cheap run caps and the FAQ-gaps list (70–72), and compaction when
the model refuses a history as too long (73), and the business's published contact details
shown to its customers (74), and real conversations replayed on every rebuild before it goes
online (75); then the running client watched nightly and after document changes, and the
same replay gating fleet offers (76–77), an OpenAI-compatible endpoint (78), skills in packs
(79), repository instructions and wrapper-aware shell rules (80), and signed hooks to the
client's systems (81).
Next: GCP and Azure profiles.
Known gaps: streaming (speech-to-speech) voice, knowledge connectors beyond files, S3,
Drive and web pages (SharePoint, Notion... push through the admin API), pgvector for very
large corpora, and a first apply of the AWS module in a real account. See
`docs/ARCHITECTURE.md` §6 and decisions 43–81.
