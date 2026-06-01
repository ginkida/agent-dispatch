"""Configuration loading and saving."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import yaml

from .models import DispatchConfig

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "agent-dispatch"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "agents.yaml"


def config_path() -> Path:
    """Return config path, respecting AGENT_DISPATCH_CONFIG env var."""
    return Path(os.environ.get("AGENT_DISPATCH_CONFIG", str(DEFAULT_CONFIG_PATH)))


def load_config(path: Path | None = None) -> DispatchConfig:
    """Load config from YAML file. Returns empty config if file missing."""
    p = path or config_path()
    if not p.exists():
        return DispatchConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if raw is None:
        return DispatchConfig()
    return DispatchConfig.model_validate(raw)


def _chmod_quiet(path: Path, mode: int) -> None:
    """Best-effort chmod. Silently ignores platforms/filesystems without it."""
    try:
        os.chmod(path, mode)
    except OSError as e:  # pragma: no cover - platform dependent
        logger.debug("chmod %s to %o failed: %s", path, mode, e)


def save_config(config: DispatchConfig, path: Path | None = None) -> None:
    """Save config to YAML file (owner-only perms — it records project paths)."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _chmod_quiet(p.parent, 0o700)
    data = config.model_dump(mode="json", exclude_none=True)
    p.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    _chmod_quiet(p, 0o600)


def _collect_mcp_servers(directory: Path) -> list[str]:
    """Collect MCP server names from all known config locations."""
    servers: list[str] = []
    for path in (
        directory / ".mcp.json",
        directory / ".claude" / "settings.local.json",
    ):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                servers.extend(data.get("mcpServers", {}).keys())
            except (json.JSONDecodeError, KeyError):
                logger.debug("Failed to parse MCP config: %s", path)
    return list(dict.fromkeys(servers))  # deduplicate, preserve order


# Public alias — callers outside config.py should use this name.
collect_mcp_servers = _collect_mcp_servers


def detect_stacks(directory: Path) -> list[str]:
    """Detect language/runtime stacks present in a project directory.

    Returns a deduplicated list of indicators like ["Python", "Docker"].
    Used by auto_describe() and by the MCP list_agents tool to surface
    capabilities cheaply (no claude subprocess needed).
    """
    indicators: list[str] = []
    if (directory / "Dockerfile").exists():
        indicators.append("Docker")
    if (directory / "docker-compose.yaml").exists() or (
        directory / "docker-compose.yml"
    ).exists():
        indicators.append("Docker Compose")
    if (directory / "Cargo.toml").exists():
        indicators.append("Rust")
    if (directory / "go.mod").exists():
        indicators.append("Go")
    if (directory / "requirements.txt").exists() or (directory / "pyproject.toml").exists():
        indicators.append("Python")
    if (directory / "package.json").exists():
        indicators.append("Node.js")
    return indicators


def detect_dbs(directory: Path) -> list[str]:
    """Detect database-related artifacts: Prisma, Alembic, generic migrations dir."""
    indicators: list[str] = []
    if (directory / "prisma").is_dir() or (directory / "schema.prisma").exists():
        indicators.append("Prisma")
    if (directory / "alembic").is_dir() or (directory / "alembic.ini").exists():
        indicators.append("Alembic")
    if (directory / "migrations").is_dir():
        indicators.append("migrations")
    return indicators


def auto_describe(directory: Path) -> str:
    """Generate agent description by reading project files.

    Produces a string like:
      MCP server for cross-project agent delegation | MCP: portainer, postgres | Python, Docker
    """
    parts: list[str] = []

    # CLAUDE.md — first meaningful lines (up to 2 sentences)
    claude_md = directory / "CLAUDE.md"
    if claude_md.exists():
        sentences: list[str] = []
        for line in claude_md.read_text(encoding="utf-8").strip().splitlines()[:40]:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not stripped.startswith("--"):
                sentences.append(stripped)
                if len(sentences) >= 2:
                    break
        if sentences:
            parts.append(" ".join(sentences))

    # README.md — fallback if no CLAUDE.md description
    if not parts:
        readme = directory / "README.md"
        if readme.exists():
            for line in readme.read_text(encoding="utf-8").strip().splitlines()[:20]:
                stripped = line.strip()
                if (
                    stripped
                    and not stripped.startswith("#")
                    and not stripped.startswith("[")
                    and not stripped.startswith("!")
                    and len(stripped) > 20
                ):
                    parts.append(stripped)
                    break

    # pyproject.toml — project description
    pyproject = directory / "pyproject.toml"
    if pyproject.exists():
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("description"):
                desc = line.split("=", 1)[1].strip().strip('"').strip("'")
                if desc:
                    parts.append(desc)
                break

    # package.json — project description
    pkg_json = directory / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            if pkg.get("description"):
                parts.append(pkg["description"])
        except (json.JSONDecodeError, KeyError):
            logger.debug("Failed to parse package.json: %s", pkg_json)

    # MCP servers — critical for understanding what tools this agent has
    servers = _collect_mcp_servers(directory)
    if servers:
        parts.append(f"MCP: {', '.join(servers)}")

    # Stack indicators (Python/Node/Rust/Go/Docker)
    stacks = detect_stacks(directory)
    if stacks:
        parts.append(f"Stack: {', '.join(stacks)}")

    # Database indicators (Prisma/Alembic/migrations)
    dbs = detect_dbs(directory)
    if dbs:
        parts.append(f"DB: {', '.join(dbs)}")

    return " | ".join(parts) if parts else f"Agent in {directory.name}"
