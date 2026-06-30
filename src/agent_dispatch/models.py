"""Data models for agent-dispatch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

_AGENT_NAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$"

KNOWN_PERMISSION_MODES = frozenset(
    {
        "default",
        "plan",
        "bypassPermissions",
    }
)


def check_permission_mode(mode: str | None) -> str | None:
    """Return a warning message if mode is unknown, else None."""
    if not mode:
        return None
    trimmed = mode.strip()
    if not trimmed:
        return None
    if trimmed not in KNOWN_PERMISSION_MODES:
        known = ", ".join(sorted(KNOWN_PERMISSION_MODES))
        return f"Unknown permission_mode: {trimmed!r}. Known values: {known}"
    return None


class AgentConfig(BaseModel):
    """Configuration for a single agent.

    `allowed_tools` / `disallowed_tools` use `None` to mean
    "inherit from settings.default_*" and `[]` to mean "explicitly empty
    (override defaults to no tools)".
    """

    directory: Path
    description: str = ""
    timeout: int = 300
    max_budget_usd: float | None = None
    model: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    capabilities: list[str] = Field(default_factory=list)
    risky_capabilities: list[str] = Field(default_factory=list)

    @field_validator("directory", mode="before")
    @classmethod
    def expand_home(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve()


class CacheSettings(BaseModel):
    """Cache configuration."""

    enabled: bool = True
    ttl: int = Field(default=300, ge=0)  # seconds; 0 effectively disables
    max_size: int = Field(default=1000, ge=1)  # entries before oldest-first eviction

    @field_validator("ttl", mode="after")
    @classmethod
    def warn_zero_ttl(cls, v: int) -> int:
        # ttl=0 is valid (entries expire immediately) but likely a mistake.
        # Let it through — cache.put() will store, cache.get() will evict.
        return v


class Settings(BaseModel):
    """Global settings for agent-dispatch."""

    default_timeout: int = 300
    default_max_budget_usd: float | None = None
    default_permission_mode: str | None = None
    default_allowed_tools: list[str] = Field(default_factory=list)
    default_disallowed_tools: list[str] = Field(default_factory=list)
    max_dispatch_depth: int = Field(default=3, ge=1)
    max_concurrency: int = Field(default=5, ge=1)
    cache: CacheSettings = Field(default_factory=CacheSettings)


def validate_agent_name(name: str) -> str:
    """Validate agent name: alphanumeric, hyphens, underscores, no leading special chars."""
    import re

    if not re.match(_AGENT_NAME_PATTERN, name):
        raise ValueError(
            f"Invalid agent name: {name!r}. "
            "Use only letters, digits, hyphens, and underscores. "
            "Must start with a letter or digit."
        )
    return name


class GroupMember(BaseModel):
    """One member of a dispatch group: a reference to an existing agent.

    `agent` is the name of an agent in `DispatchConfig.agents`. `use_for` is a
    short, group-contextual hint ("dispatch me when...") that helps the
    orchestrating LLM route within the group. It is descriptive only — never
    passed to the `claude` CLI.
    """

    agent: str
    use_for: str = ""

    @field_validator("agent")
    @classmethod
    def _valid_agent(cls, v: str) -> str:
        return validate_agent_name(v)


class DispatchGroup(BaseModel):
    """A named, descriptive group of agents for coordinated cross-project work.

    A group is a *layer*, not an execution engine: there is no router and no
    state machine — the orchestrating LLM coordinates using the normal dispatch
    tools. The two text fields target two different audiences:

    - `description` is ORCHESTRATOR-facing (how to coordinate the group, who to
      call for what). It is surfaced by list_groups/inspect_group but is NEVER
      injected into a member's prompt.
    - `shared_context` is MEMBER-facing FACTS (stack names, counter ids,
      conventions) that hold regardless of which member reads them. It is
      auto-injected into dispatches made with `group=`.

    `members` reference agents by name. Membership is many-to-many: a shared
    gateway agent (e.g. infra, analytics) can belong to several groups.
    """

    description: str = ""
    shared_context: str = ""
    members: list[GroupMember] = Field(default_factory=list)


class DispatchConfig(BaseModel):
    """Top-level config: agents + groups + settings."""

    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    groups: dict[str, DispatchGroup] = Field(default_factory=dict)
    settings: Settings = Field(default_factory=Settings)

    @model_validator(mode="after")
    def _validate_group_names(self) -> DispatchConfig:
        # Validate only the group KEYS (cheap, keeps prompt-label construction
        # provably safe regardless of how the YAML was hand-authored).
        # Deliberately does NOT check that each member's agent exists — gateway
        # agents are shared and a hard cross-ref check would make removing one
        # brick config load (every CLI command + MCP call dies in load_config).
        # Dangling refs are flagged at read time (list_groups/inspect_group) and
        # blocked at CLI mutation time instead.
        for name in self.groups:
            validate_agent_name(name)
        return self


class DispatchResult(BaseModel):
    """Result of a dispatch call."""

    agent: str
    success: bool
    result: str
    session_id: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    error: str | None = None
    error_type: str | None = None  # permission, timeout, recursion, not_found, cli_error
    # Set when response_format="json" was requested AND the agent's result
    # parsed cleanly. None means: not requested, or requested but unparseable.
    parsed_result: Any | None = None
    # Tools the claude CLI refused to run (from `permission_denials` in its
    # JSON output). Non-empty even on success=True — the agent may have
    # completed with an incomplete answer because a tool was blocked.
    denied_tools: list[str] | None = None
    # Advisory, non-fatal guidance (e.g. "result may be incomplete, grant X").
    # Errors stay in `error`; hint is for successful-but-degraded results.
    hint: str | None = None
    # True when cost_usd exceeded the agent's max_budget_usd (or the settings
    # default). Post-hoc only — the money is already spent, the dispatch is
    # NOT failed for it. None means: no budget configured, or within budget.
    budget_exceeded: bool | None = None
