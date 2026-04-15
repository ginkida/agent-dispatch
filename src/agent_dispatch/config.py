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


def save_config(config: DispatchConfig, path: Path | None = None) -> None:
    """Save config to YAML file."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json", exclude_none=True)
    p.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


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

    # Stack indicators
    indicators = []
    if (directory / "Dockerfile").exists():
        indicators.append("Docker")
    if (directory / "docker-compose.yaml").exists() or (directory / "docker-compose.yml").exists():
        indicators.append("Docker Compose")
    if (directory / "Cargo.toml").exists():
        indicators.append("Rust")
    if (directory / "go.mod").exists():
        indicators.append("Go")
    if (directory / "requirements.txt").exists() or pyproject.exists():
        indicators.append("Python")
    if pkg_json.exists():
        indicators.append("Node.js")
    if indicators:
        parts.append(f"Stack: {', '.join(indicators)}")

    # Database indicators
    db_indicators = []
    if (directory / "prisma").is_dir() or (directory / "schema.prisma").exists():
        db_indicators.append("Prisma")
    if (directory / "alembic").is_dir() or (directory / "alembic.ini").exists():
        db_indicators.append("Alembic")
    if (directory / "migrations").is_dir():
        db_indicators.append("migrations")
    if db_indicators:
        parts.append(f"DB: {', '.join(db_indicators)}")

    return " | ".join(parts) if parts else f"Agent in {directory.name}"
