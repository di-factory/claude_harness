# claude_harness — Architecture Proposal

Status: **Accepted (v1 decisions recorded in §7)** · 2026-09-27

## 1. What we mean by "harness"

An *agent harness* is everything around the model that turns a stateless
completion API into an agent that does work: the loop, tools, context
management, persistence, permissions, hooks and observability. The model
reasons; the harness executes, remembers and constrains.

The production harnesses studied (Claude Code / Claude Agent SDK, OpenAI Agents
SDK, Microsoft Agent Framework, Pydantic AI, LangGraph, and the open-source
coding agents surveyed in recent literature) converge on the same set of
components:

1. The agent loop (call model → run tools → feed results back → repeat)
2. Model/provider adapter
3. Tool registry + execution (incl. MCP)
4. Context management (budgeting, compaction, just-in-time retrieval)
5. Session persistence / resume
6. System-prompt assembly (base prompt, project memory, skills)
7. Lifecycle hooks
8. Permissions / sandboxing / human-in-the-loop
9. Sub-agents / orchestration
10. Observability + evals

## 2. Options considered

| | A. Build on a framework (LangGraph / Pydantic AI / MAF) | B. Wrap the Claude Agent SDK | C. Own core loop, thin adapters (**recommended**) |
|---|---|---|---|
| Time to first demo | Fast | Fastest | Medium (~a few hundred LOC for the core) |
| Control over loop & context | Limited by framework abstractions | Low — the loop lives in the SDK/CLI | Full |
| Provider portability | Good | Claude only | Good, via adapter protocol |
| Debuggability | Framework stack traces, graph DSL | Opaque subprocess | Plain Python |
| Dependency / churn risk | High (fast-moving APIs) | Medium | Low |
| Learning value / ownership | Low | Low | High |

**Why C.** Anthropic's own engineering guidance is consistent here: prefer a
hand-rolled loop over framework abstractions, a small set of high-signal tools,
just-in-time retrieval over pre-indexed RAG, orchestrator–worker sub-agents,
and explicit context budgeting. The loop itself is small; the value of a
harness is in context management, tools, and safety — exactly the parts
frameworks tend to hide. Every harness component encodes an assumption about
what the model *can't* do; those assumptions expire as models improve, so the
components must be cheap to change or delete. Owning the core keeps them cheap.

We still borrow libraries where they are clearly best-in-class (Pydantic for
schemas, the official `anthropic` SDK for transport, the `mcp` SDK for MCP).

## 3. Proposed architecture

```
                ┌──────────────────────────────────────────────┐
  Interfaces    │  TUI (Textual)  │  Python API  │  (later: CLI/HTTP) │
                └────────────────────────┬─────────────────────┘
                                         │ events (async stream)
                ┌────────────────────────▼─────────────────────┐
  Core          │                 Agent Loop                    │
                │  run(session) -> AsyncIterator[Event]         │
                │   ├─ PromptBuilder  (system prompt, memory,   │
                │   │                  skills)                  │
                │   ├─ ContextManager (token budget, compaction,│
                │   │                  tool-result truncation)  │
                │   ├─ HookBus        (pre/post tool, on_stop…) │
                │   └─ PermissionPolicy (allow / ask / deny)    │
                └───────┬───────────────────────┬───────────────┘
                        │                       │
          ┌─────────────▼──────────┐  ┌─────────▼──────────────────┐
 Adapters │ ModelProvider protocol │  │ ToolRegistry               │
          │  - AnthropicProvider   │  │  - @tool python functions  │
          │  - OpenAIProvider      │  │  - built-ins: read/write/  │
          │  - FakeProvider (tests)│  │    edit/bash/grep/glob     │
          └────────────────────────┘  │  - MCP client tools        │
                                      │  - SubAgent tool           │
                                      └─────────┬──────────────────┘
                                                │
                ┌───────────────────────────────▼──────────────┐
  Infra         │ SessionStore (append-only JSONL event log)   │
                │ Sandbox/Executor (subprocess, cwd jail,      │
                │   optional container)                        │
                │ Telemetry (structured logs, OpenTelemetry)   │
                └──────────────────────────────────────────────┘
```

### 3.1 Core data model (provider-neutral)

- `Message(role, content: list[Block])` where `Block` is `Text | ToolUse |
  ToolResult | Thinking | Image`.
- `Event` — everything the loop emits (`TextDelta`, `ToolCallStarted`,
  `ToolCallFinished`, `PermissionRequested`, `Compacted`, `TurnEnded`,
  `Error`). UIs, logging and persistence all consume the same event stream.
- `Session` — id, cwd, config, message history; reconstructible by replaying
  its event log (gives resume + fork for free).

### 3.2 Agent loop

```python
async def run(session, user_input) -> AsyncIterator[Event]:
    session.append(user(user_input))
    while True:
        ctx = context_manager.prepare(session)          # budget / compact
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

Async-first (`asyncio`) so parallel tool calls, streaming and sub-agents are
natural. Stop conditions: no tool calls, max turns, max tokens/cost, user
interrupt.

### 3.3 Model providers

A small `Protocol` (`stream(messages, tools, **opts)`, `count_tokens`,
`capabilities`). The harness is **provider-agnostic by design**: the core only
sees the neutral message/event model, never a vendor SDK type. Provider-specific
features (prompt caching, extended thinking, server tools) are exposed through
a `capabilities` descriptor so the core can use them when present and degrade
gracefully when not.

M1 ships the `AnthropicProvider` plus a `FakeProvider` that replays scripted
responses for deterministic tests. OpenAI and local models (Ollama) follow as
additional adapters; a shared conformance test suite runs against every
provider to keep them interchangeable.

### 3.4 Tools

Tools are grouped into **tool packs** so the same core serves both coding and
general-purpose agents. A pack bundles tools, prompt fragments and default
permission rules, and is enabled per session/profile:

- `coding` — read/write/edit files, bash, grep, glob.
- `general` — web fetch/search, HTTP, scratch notes.
- Third-party packs register via Python entry points; MCP servers appear as packs too.

- Declared with a decorator; JSON schema generated from type hints via Pydantic.
- Each tool declares `read_only: bool` and a permission category, so the
  policy can auto-allow reads and gate writes/shell.
- Output is truncated/summarised by the ContextManager, never dumped raw.
- MCP servers (stdio + streamable HTTP) are mounted as tool namespaces.
- Sub-agent = a tool that runs a child `Session` with its own context and a
  restricted tool set, returning only its final answer (orchestrator–worker).

### 3.5 Context management

- Token budget per request; prompt-caching breakpoints placed on stable prefix
  (system prompt + tools + early history).
- Compaction when approaching the limit: summarise older turns into a
  structured summary block; keep recent turns verbatim.
- Just-in-time retrieval: agents read files/grep on demand rather than
  pre-loading; large tool results are stored and referenced.
- Project memory file (`HARNESS.md` at the project root) loaded into the system prompt.

### 3.6 Safety

- `PermissionPolicy`: rules like `allow: read_*`, `ask: bash(*)`,
  `deny: bash(rm -rf *)`, configurable per project/user.
- `ask` produces a `PermissionRequested` event; the interface answers.
- Execution goes through a pluggable `Executor` interface. v1 ships
  `SubprocessExecutor` (workspace-confined cwd, path checks on file tools,
  timeouts, output limits). A `ContainerExecutor` (Docker/Podman) is added
  later behind the same interface without touching tools or the loop.

### 3.7 Hooks

Named lifecycle points (`session_start`, `pre_tool`, `post_tool`,
`pre_compact`, `stop`) accepting Python callables or shell commands. Hooks can
veto, modify input, or inject context.

### 3.8 Observability & evals

- Every event goes to the JSONL log → replay, debugging, cost accounting.
- Optional OpenTelemetry spans (one per turn and per tool call).
- `evals/` folder with task fixtures run against `FakeProvider` (unit) and real
  models (integration) from day one.

### 3.9 Memory subsystem (5 layers)

Adopted from *Agent Memory — The 5-Layer Playbook* (based on the CoALA
framework). Sessions end, but the knowledge from them persists. Memory is a
core subsystem behind a `MemoryBackend` protocol so it can be swapped later
for Mem0/Zep, and every store is **scoped per project** so nothing leaks
between projects.

| Layer | Holds | Harness component | Storage (v1) | Expiry |
|---|---|---|---|---|
| 1. Working | Current context | `ContextManager` | context window | end of call |
| 2. Episodic | What happened: task, approach, outcome, errors, fixes, user corrections | `EpisodicStore` | `.harness/memory/episodes.jsonl` (distilled from the session event log) | 30–90 d TTL, pins |
| 3. Semantic | What is true: facts, preferences, entities | `SemanticStore` + ontology | `.harness/memory/facts.db` (entity, type, relation, value, source_episode, status, superseded_by) | on supersession |
| 4. Procedural | How to do things | Skills (same format as tool-pack skills) | `.harness/memory/skills/*.md`, versioned | on version update |
| 5. Forgetting | What to delete | `ForgettingEngine` | — | runs at session start / on schedule |

**Data flow**

```
working ──overflow (pre_compact hook)──► episodic ──distil──► semantic ──encode──► procedural
   ▲                                        │                   │                   │
   └──────────── retrieval (session_start / memory_search tool) ◄───────────────────┘
                     forgetting engine prunes episodic / semantic / procedural
```

- **Overflow**: before compaction the ContextManager extracts decisions and
  facts into episodic memory, so important context is saved rather than
  truncated.
- **Retrieval**: at `session_start` the prompt builder injects a small,
  budgeted memory block: top-k similar episodes, relevant facts, and matching
  skill triggers. The model can also call the `memory_search` and
  `memory_write` tools.
- **Write-back**: at `stop` an Episode record is saved and candidate facts
  are extracted, validated against the ontology, then resolved against
  existing facts (duplicate → merge, changed → supersede, both current →
  flag).
- **Forgetting**: expiry by TTL, supersession, and contradiction flagging.
  Superseded or expired items go to an archive; they are not deleted
  outright.

**Guardrails (where this design deliberately departs from the playbook)**

- Skills are *loaded into context*; the model still drives. There is no
  "execute the skill without the LLM" shortcut.
- Promoting a method to a skill (after ≥3 successes) creates a **proposal
  the user approves**. A skill is persistent instructions, so an
  auto-written skill would be a way to make prompt injection persist.
- Contradictions are shown to the user; they are never resolved silently.
- The memory block has a token budget, so memory cannot crowd out the task.

**Tests**: amnesia (recall across sessions), contradiction, staleness (TTL),
skill promotion, isolation (no cross-project leakage), and load (10k episodes
retrieved in under 500 ms).

## 4. Tech stack

| Concern | Choice |
|---|---|
| Python | 3.12+ (MIT, PyPI: `di-factory-general-harness`) |
| Packaging / env | `uv`, `pyproject.toml` |
| Schemas / config | Pydantic v2; JSON settings files (user + project) |
| Memory store | SQLite (stdlib `sqlite3`) + FTS5 |
| LLM transport | `anthropic` SDK (first), `openai` SDK (optional extra) |
| MCP | official `mcp` Python SDK |
| UI | Textual (TUI) first; Typer entry point to launch it |
| Tests | pytest + pytest-asyncio, FakeProvider |
| Lint / types | ruff, mypy (strict on `core/`) |

## 5. Proposed layout

```
src/dif_general_harness/
  core/        loop.py  events.py  messages.py  session.py  context.py
  providers/   base.py  anthropic.py  openai.py  fake.py
  tools/       registry.py  builtin/ (fs.py, shell.py, search.py)  mcp.py  subagent.py
  policy/      permissions.py  hooks.py
  store/       jsonl.py
  memory/      base.py  episodic.py  semantic.py  procedural.py  forgetting.py  ontology.py
  prompts/     system.md  builder.py
  tools/packs/ coding/  general/
  tui/         app.py  widgets/  (Textual)
tests/
evals/
docs/
```

## 6. Roadmap

1. **M0 – skeleton**: data model, FakeProvider, loop, JSONL store, tests.
2. **M1 – usable agent**: Anthropic provider (streaming, caching), coding +
   general tool packs, SubprocessExecutor, permission prompts, Textual TUI.
3. **M2 – long sessions & memory I**: compaction with an overflow handler,
   resume/fork, project memory file, hooks, episodic + semantic memory with
   retrieval at session start.
4. **M3 – extensibility & memory II**: MCP client, sub-agents, skills,
   procedural memory (promotion approved by the user), forgetting engine,
   ontology.
5. **M4 – more providers & hardening**: OpenAI + Ollama adapters with
   conformance suite, ContainerExecutor, OpenTelemetry, eval suite.

## 7. Decisions (v1)

| # | Question | Decision |
|---|---|---|
| 1 | Primary use case | **Both, pluggable** — neutral core + tool packs (`coding`, `general`, MCP, third-party). |
| 2 | Providers | **Multi-provider architecture now**; M1 implements **Anthropic** only (+ FakeProvider). OpenAI/Ollama next. |
| 3 | First interface | **TUI** (Textual), built on the public Python API / event stream. |
| 4 | Sandboxing | **Pluggable Executor**; start with workspace-confined subprocess + permission prompts, containers later. |
| 5 | Memory | **5-layer memory** (working / episodic / semantic / procedural / forgetting), project-scoped, behind `MemoryBackend`; see §3.9. |
| 6 | Memory retrieval | **SQLite FTS5 (BM25) keyword search** in v1; embeddings added later behind the same interface. |
| 7 | State location | **Split**: project-scoped sessions/memory/config in `<project>/.harness/`; user-global settings & credentials in `~/.harness/`. |
| 8 | Multi-agent | **Orchestrator + isolated sub-agents** (sub-agent is a tool; returns only its final answer). No shared-ledger teams in v1. |
| 9 | Config format | **JSON** (`.harness/settings.json`, `~/.harness/settings.json`), validated by Pydantic; project overrides user. |
| 10 | Python | **3.12+** |
| 11 | License / distribution | **MIT**, published to **PyPI**. |
| 12 | Naming | PyPI dist **`di-factory-general-harness`**, import package **`dif_general_harness`**, CLI command **`dif-general-harness`**. |
| 13 | Default permissions | Read-only tools auto-allowed; **file writes, shell and network ask** (with "always allow" persisted to settings). |
| 14 | Observability | JSONL session log always; **OpenTelemetry** (GenAI semantic conventions) as an opt-in extra. |
| 15 | Credentials | `ANTHROPIC_API_KEY` env var, falling back to `~/.harness/credentials.json` (mode 600). Never stored in project settings. |

## References

- Anthropic — Building Effective Agents; Effective harnesses for long-running
  agents; Harness design for long-running application development; Demystifying
  evals for AI agents (anthropic.com/engineering)
- Microsoft Agent Framework at BUILD 2026 (devblogs.microsoft.com/agent-framework)
- Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents
  (arXiv 2609.00006)
- The Anatomy of an Agent Harness (blog.dailydoseofds.com)
- awesome-harness-engineering (github.com/ai-boost/awesome-harness-engineering)
- Agent Memory — The 5-Layer Playbook (independent compilation, Sep 2026;
  its quoted metrics are not independently verified)
- Sumers et al., Cognitive Architectures for Language Agents (CoALA)
- Pydantic AI comparisons (pydantic.dev/docs/ai/comparisons)
