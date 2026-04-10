"""Data models for agent-dispatch."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator

_AGENT_NAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$"


class AgentConfig(BaseModel):
    """Configuration for a single agent."""

    directory: Path
    description: str = ""
    timeout: int = 300
    max_budget_usd: float | None = None
    model: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)

    @field_validator("directory", mode="before")
    @classmethod
    def expand_home(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve()


class CacheSettings(BaseModel):
    """Cache configuration."""

    enabled: bool = True
    ttl: int = Field(default=300, ge=0)  # seconds; 0 effectively disables

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


class DispatchConfig(BaseModel):
    """Top-level config: agents + settings."""

    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    settings: Settings = Field(default_factory=Settings)


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
