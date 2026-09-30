"""Exercise an installed wheel through its CLI and real MCP stdio transport.

Run with the clean virtual environment's Python, using -I to exclude the checkout.
All state is temporary; no Claude command or paid dispatch is invoked.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import agent_dispatch
from agent_dispatch.jobs import JobStore
from agent_dispatch.models import DispatchResult


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def cli(binary: str, env: dict[str, str], *args: str, exit_code: int = 0) -> str:
    result = subprocess.run(
        [binary, *args], env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    require(
        result.returncode == exit_code,
        f"CLI {args!r} exited {result.returncode}: {result.stdout}\n{result.stderr}",
    )
    return result.stdout


async def check_mcp(binary: str, env: dict[str, str], job_id: str, output: str) -> None:
    parameters = StdioServerParameters(command=binary, args=["serve"], env=env)
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            require("dispatch_async" in tools, "Async dispatch tool is missing")
            require(
                "dry_run" in tools["dispatch_gc"].inputSchema["properties"],
                "GC preview is missing from the MCP schema",
            )

            async def call(name: str, arguments: dict) -> object:
                response = await session.call_tool(name, arguments)
                require(not response.isError, f"MCP {name} failed: {response}")
                return json.loads(response.content[0].text)

            agents = await call("list_agents", {})
            require(agents[0]["name"] == "infra", "MCP cannot read the CLI-created agent")
            jobs = await call("dispatch_jobs", {"agent": "infra"})
            require([job["id"] for job in jobs] == [job_id], "MCP job history differs from CLI")
            result = await call("fetch_result", {"ref": job_id})
            require(result["result"] == output, "MCP lost full Unicode result text")
            preview = await call("dispatch_gc", {"max_age_days": 7, "dry_run": True})
            require(preview["purged"] == 0, "MCP preview reports deletion")
            require(preview["job_ids"] == [job_id], "MCP preview missed the old job")


def main() -> None:
    require(sys.prefix != sys.base_prefix, "Run this check using a clean virtual environment")
    location = Path(agent_dispatch.__file__).resolve()
    require(
        location.is_relative_to(Path(sys.prefix).resolve()),
        f"Expected a wheel installed in this virtual environment, got {location}",
    )
    binary = str(Path(sysconfig.get_path("scripts")) / "agent-dispatch")
    with tempfile.TemporaryDirectory(prefix="agent-dispatch-install-") as directory:
        root = Path(directory)
        env = dict(
            os.environ,
            PATH=directory,  # Claude cannot be discovered, even on a developer machine.
            AGENT_DISPATCH_CONFIG=str(root / "agents.yaml"),
            AGENT_DISPATCH_JOBS_DIR=str(root / "jobs"),
            AGENT_DISPATCH_USAGE_LOG=str(root / "usage.jsonl"),
            AGENT_DISPATCH_SERVERS_DIR=str(root / "servers"),
        )
        require(json.loads(cli(binary, env, "jobs", "--json")) == [], "Fresh history is not empty")
        report = json.loads(cli(binary, env, "doctor", "--json", "--strict", exit_code=1))
        require(report["status"] == "fail", "Doctor did not report the deliberately missing CLI")
        project = root / "проект"
        project.mkdir()
        cli(binary, env, "add", "infra", str(project), "--description", "Installation smoke check")
        store = JobStore(root / "jobs")
        output = "Полный результат\n" * 1000
        job = store.create_completed(
            "infra", "Проверить установку",
            DispatchResult(agent="infra", success=True, result=output),
        )
        job.completed_at = 1
        store._write(job)
        path = root / "jobs" / f"{job.id}.json"
        before = path.read_bytes()
        exported = json.loads(cli(binary, env, "job", job.id, "--json"))
        require(exported["result"]["result"] == output, "CLI truncated the full result")
        require(
            job.id in cli(binary, env, "gc", "--days", "7", "--dry-run"),
            "CLI preview missed the old job",
        )
        asyncio.run(asyncio.wait_for(check_mcp(binary, env, job.id, output), timeout=30))
        require(path.read_bytes() == before, "Read-only checks changed the job record")
    print("Installed wheel passed: CLI, full Unicode result, GC preview, and MCP stdio transport.")


if __name__ == "__main__":
    main()
