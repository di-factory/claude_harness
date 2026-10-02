"""Pydantic schema for Solution Spec v1 (docs/spec/SOLUTION_SPEC.md).

Structural sections forbid unknown keys, so typos fail loudly. Free-form
payloads (provider settings, tool-pack config, step bodies, check bodies) allow
extra keys while the spec is still a draft.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MODEL_ROLES = {
    "main",
    "subagent",
    "verifier",
    "compaction",
    "memory_extraction",
    "embedding",
    "router",
    "title",
    "ocr",
}
_ID = r"^[a-z0-9][a-z0-9._-]*$"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --- solution, tenant, variables -------------------------------------------------


class SolutionInfo(Strict):
    id: str = Field(pattern=_ID)
    version: str
    name: str | None = None
    lob: str
    description: str | None = None
    locale: str = "en"

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not _SEMVER.match(v):
            raise ValueError(f"version {v!r} is not semver (x.y.z)")
        return v


class Tenant(Strict):
    id: str = Field(pattern=_ID)
    name: str
    timezone: str = "UTC"


class Ask(Strict):
    question: str
    answered_by: Literal["client", "difactory", "either"] = "either"
    example: str | None = None
    group: str | None = None
    order: int | None = None


class Variable(Strict):
    type: Literal[
        "string",
        "integer",
        "number",
        "boolean",
        "enum",
        "list",
        "object",
        "duration",
        "schedule",
        "file",
    ]
    required: bool = False
    default: Any = None
    description: str | None = None
    min: float | None = None
    max: float | None = None
    options: list[Any] | None = None
    ask: Ask | None = None


class SecretDecl(Strict):
    description: str


# --- models, agents, tools -------------------------------------------------------


class ModelRoleConfig(Strict):
    provider: str
    model: str
    effort: Literal["low", "medium", "high"] = "medium"


class Models(Strict):
    roles: dict[str, ModelRoleConfig]
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    allowed_regions: list[str] | None = None

    @field_validator("roles")
    @classmethod
    def _known_roles(cls, roles: dict[str, ModelRoleConfig]) -> dict[str, ModelRoleConfig]:
        unknown = set(roles) - MODEL_ROLES
        if unknown:
            raise ValueError(
                f"unknown model roles {sorted(unknown)}; allowed: {sorted(MODEL_ROLES)}"
            )
        return roles


class Agent(Strict):
    description: str | None = None
    prompt: str
    model_role: str
    tools: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    memory: dict[str, Any] | None = None
    subagents: list[str] = Field(default_factory=list)
    handoffs: list[str] = Field(default_factory=list)
    max_turns: int | None = Field(default=None, ge=1)
    context_tokens: int | None = Field(default=None, ge=2000)  # compact the history beyond it
    output_schema: str | None = None
    workspace: str | None = None
    budgets: dict[str, Any] | None = None


class HttpOperation(Strict):
    method: str
    path: str
    effect: Literal["read", "write", "external"]
    input: dict[str, Any] = Field(default_factory=dict)
    verify: str | None = None


class HttpConnector(Strict):
    base_url: str
    auth: dict[str, Any] = Field(default_factory=dict)
    operations: dict[str, HttpOperation]


class McpServer(Strict):
    transport: Literal["stdio", "http"]
    url: str | None = None
    command: list[str] | None = None
    auth: Any = None


class ToolOverride(Strict):
    permission: Literal["allow", "ask", "deny"] | None = None
    verify: str | None = None
    effect: Literal["read", "write", "external"] | None = None


class Tools(Strict):
    packs: list[str] = Field(default_factory=list)
    config: dict[str, dict[str, Any]] = Field(default_factory=dict)
    mcp: dict[str, McpServer] = Field(default_factory=dict)
    http: dict[str, HttpConnector] = Field(default_factory=dict)
    python: list[str] = Field(default_factory=list)
    overrides: dict[str, ToolOverride] = Field(default_factory=dict)


# --- knowledge, memory, channels, triggers, workflows -----------------------------


class Knowledge(Strict):
    corpora: dict[str, dict[str, Any]] = Field(default_factory=dict)


class Memory(Strict):
    layers: list[Literal["episodic", "semantic", "procedural"]] = Field(default_factory=list)
    scope: Literal["contact", "instance", "agent"] = "instance"
    episodic_ttl: str | None = None
    skill_promotion: dict[str, Any] | None = None
    shared_ledger: bool = False


class Template(Strict):
    file: str
    provider_template_id: str | None = None


class Channel(Strict):
    type: Literal["gateway", "telegram", "web", "email", "slack", "api", "voice"]
    provider: str | None = None
    credentials: Any = None
    address: str | None = None
    entry_agent: str | None = None
    contact_key: str | None = None
    session_window: str | None = None
    templates: dict[str, Template] = Field(default_factory=dict)
    purpose: Literal["contact", "hitl", "founder", "outbound"] = "contact"
    reply_via: str | None = None
    enabled: bool | str = True
    voice: dict[str, Any] | None = None  # voice channels: language, voice, greeting...
    public: bool = False  # web channels only: anyone with the link may chat (rate-limited)


class Trigger(Strict):
    type: Literal["schedule", "relative", "delay", "webhook", "event", "file", "batch"]
    cron: str | None = None
    source: Any = None
    offset: str | None = None
    after: str | None = None
    started_by: str | None = None
    unless: str | None = None
    when: str | None = None
    path: str | None = None
    auth: Any = None
    event: str | None = None
    match: list[str] | None = None
    dedupe_key: str | None = None
    workflow: str | None = None
    agent: str | None = None
    input: Any = None
    requires_consent: bool = False
    channel: str | None = None  # deliver the agent's reply here (e.g. a founder's daily brief)
    to: str | None = None  # the address on that channel; default: the channel's address


class Step(Loose):
    id: str
    type: Literal[
        "agent",
        "tool",
        "template",
        "message",
        "approval",
        "wait",
        "branch",
        "parallel",
        "handoff",
        "timer",
        "end",
    ]
    when: str | None = None


class Workflow(Strict):
    input: dict[str, Any] | None = None
    steps: list[Step]
    concurrency: int | None = Field(default=None, ge=1)
    on_error: str | None = None


# --- policies, governance, hitl, evals, deploy -------------------------------------


class Permissions(Strict):
    allow: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class Check(Loose):
    type: Literal["tool", "condition", "citations", "verifier", "command"]


class Verification(Strict):
    checks: dict[str, Check] = Field(default_factory=dict)
    verifier: dict[str, Any] | None = None


class Policies(Strict):
    profile: Literal["strict", "default", "fast"] = "default"
    permissions: Permissions = Field(default_factory=Permissions)
    budgets: dict[str, Any] = Field(default_factory=dict)
    verification: Verification = Field(default_factory=Verification)
    escalation: dict[str, Any] | None = None
    router: dict[str, Any] | None = None


class Pii(Strict):
    classes: list[str] = Field(default_factory=list)
    tokenize: bool = True
    reveal_in_output: list[str] = Field(default_factory=list)
    reveal_to_tools: dict[str, list[str]] = Field(default_factory=dict)
    override_reason: str | None = None


class Consent(Strict):
    required: bool = False
    channels: list[str] = Field(default_factory=list)
    opt_out_keywords: list[str] = Field(default_factory=list)


class Governance(Strict):
    pii: Pii = Field(default_factory=Pii)
    consent: Consent = Field(default_factory=Consent)
    retention: dict[str, str] = Field(default_factory=dict)
    audit: dict[str, Any] = Field(default_factory=dict)
    regions: dict[str, Any] = Field(default_factory=dict)
    compliance: list[str] = Field(default_factory=list)


class Hitl(Strict):
    approvers: list[str] = Field(default_factory=list)
    notify: list[dict[str, Any]] = Field(default_factory=list)
    approval_timeout: str | None = None
    on_timeout: Literal["reject", "escalate", "approve"] | None = None


class Evals(Strict):
    suites: list[str] = Field(default_factory=list)
    thresholds: dict[str, float] = Field(default_factory=dict)


class Deploy(Strict):
    target: Literal["local", "docker", "aws"]
    profile: str | None = None
    region: str | None = None
    secrets_backend: Literal[
        "env", "file", "aws-secrets-manager", "gcp-secret-manager", "1password"
    ] = "env"
    database: Literal["sqlite", "postgres"] | None = None


class Ledger(Strict):
    backend: Literal["builtin", "linear", "notion"] = "builtin"
    fields: dict[str, str] = Field(default_factory=dict)
    visible_to: list[str] = Field(default_factory=list)


# --- the spec --------------------------------------------------------------------


class BrandColors(Strict):
    primary: str | None = None  # "#1f5f4a": buttons, headings, the chat
    accent: str | None = None  # a second color for highlights


class Branding(Strict):
    """The client's look on the landing page and the chat (see constructor/brand.py)."""

    colors: BrandColors | None = None
    logo: str | None = None  # a data:image/...;base64 URI, so it is signed with the solution


class SolutionSpec(Strict):
    schema_: str | None = Field(default=None, alias="$schema")
    spec_version: Literal["1"]
    kind: Literal["pack", "instance"]
    solution: SolutionInfo
    extends: list[str] = Field(default_factory=list)
    tenant: Tenant | None = None
    variables: dict[str, Variable] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, SecretDecl] = Field(default_factory=dict)
    models: Models | None = None
    agents: dict[str, Agent] = Field(default_factory=dict)
    tools: Tools = Field(default_factory=Tools)
    knowledge: Knowledge = Field(default_factory=Knowledge)
    memory: Memory | None = None
    channels: dict[str, Channel] = Field(default_factory=dict)
    triggers: dict[str, Trigger] = Field(default_factory=dict)
    workflows: dict[str, Workflow] = Field(default_factory=dict)
    policies: Policies = Field(default_factory=Policies)
    governance: Governance = Field(default_factory=Governance)
    hitl: Hitl | None = None
    evals: Evals = Field(default_factory=Evals)
    deploy: Deploy | None = None
    workspaces: dict[str, dict[str, Any]] = Field(default_factory=dict)
    ledger: Ledger | None = None
    extensions: list[str] = Field(default_factory=list)
    branding: Branding | None = None


TOP_LEVEL_KEYS = {f.alias or name for name, f in SolutionSpec.model_fields.items()}
