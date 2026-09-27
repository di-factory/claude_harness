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
- Project memory file (e.g. `HARNESS.md`) loaded into the system prompt.

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

## 4. Tech stack

| Concern | Choice |
|---|---|
| Python | 3.12+ |
| Packaging / env | `uv`, `pyproject.toml` |
| Schemas / config | Pydantic v2, `pydantic-settings` |
| LLM transport | `anthropic` SDK (first), `openai` SDK (optional extra) |
| MCP | official `mcp` Python SDK |
| UI | Textual (TUI) first; Typer entry point to launch it |
| Tests | pytest + pytest-asyncio, FakeProvider |
| Lint / types | ruff, mypy (strict on `core/`) |

## 5. Proposed layout

```
src/harness/
  core/        loop.py  events.py  messages.py  session.py  context.py
  providers/   base.py  anthropic.py  openai.py  fake.py
  tools/       registry.py  builtin/ (fs.py, shell.py, search.py)  mcp.py  subagent.py
  policy/      permissions.py  hooks.py
  store/       jsonl.py
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
3. **M2 – long sessions**: compaction, resume/fork, project memory, hooks.
4. **M3 – extensibility**: MCP client, sub-agents, skills.
5. **M4 – more providers & hardening**: OpenAI + Ollama adapters with
   conformance suite, ContainerExecutor, OpenTelemetry, eval suite.

## 7. Decisions (v1)

| # | Question | Decision |
|---|---|---|
| 1 | Primary use case | **Both, pluggable** — neutral core + tool packs (`coding`, `general`, MCP, third-party). |
| 2 | Providers | **Multi-provider architecture now**; M1 implements **Anthropic** only (+ FakeProvider). OpenAI/Ollama next. |
| 3 | First interface | **TUI** (Textual), built on the public Python API / event stream. |
| 4 | Sandboxing | **Pluggable Executor**; start with workspace-confined subprocess + permission prompts, containers later. |

## References

- Anthropic — Building Effective Agents; Effective harnesses for long-running
  agents; Harness design for long-running application development; Demystifying
  evals for AI agents (anthropic.com/engineering)
- Microsoft Agent Framework at BUILD 2026 (devblogs.microsoft.com/agent-framework)
- Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents
  (arXiv 2609.00006)
- The Anatomy of an Agent Harness (blog.dailydoseofds.com)
- awesome-harness-engineering (github.com/ai-boost/awesome-harness-engineering)
- Pydantic AI comparisons (pydantic.dev/docs/ai/comparisons)
