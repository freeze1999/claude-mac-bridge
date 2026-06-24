#!/usr/bin/env python3
"""
Claude Mac Bridge, MCP Server
Exposes an `ask_claude` tool so an AI agent can delegate tasks to Claude Code
running on a remote Mac via SSH over Tailscale.

Configuration (env vars):
  CLAUDE_BRIDGE_SSH_HOST   SSH target, e.g. user@100.x.x.x  (required)
  CLAUDE_BRIDGE_CLAUDE_BIN Path to claude binary on the Mac  (default: claude)
  CLAUDE_BRIDGE_TIMEOUT    Seconds before timeout            (default: 600)
  CLAUDE_BRIDGE_LOG_PATH   Path to delegation audit log      (default: ./bridge.log)
"""

import asyncio
import json
import os
import shlex
import time
import uuid
from datetime import datetime, timezone

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

MAC_HOST       = os.environ.get("CLAUDE_BRIDGE_SSH_HOST", "")
MAC_CLAUDE     = os.environ.get("CLAUDE_BRIDGE_CLAUDE_BIN", "claude")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_BRIDGE_TIMEOUT", "600"))
SSH_OPTS       = ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
                  "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
LOG_FILE       = os.environ.get("CLAUDE_BRIDGE_LOG_PATH",
                   os.path.join(os.path.dirname(__file__), "bridge.log"))

app = Server("claude-bridge")


async def _log_claude_version():
    """Log the Claude Code CLI version at startup for debugging."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", *SSH_OPTS, MAC_HOST, f"{MAC_CLAUDE} --version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        version = out.decode().strip().splitlines()[0] if out else "unknown"
        log_event({"event": "startup", "claude_version": version,
                   "ts": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        log_event({"event": "startup", "claude_version": f"error: {e}",
                   "ts": datetime.now(timezone.utc).isoformat()})


def log_event(event: dict):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")
            f.flush()
    except Exception:
        pass


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="ask_claude",
            description=(
                "Delegate a task to Claude Code running on a remote Mac via SSH over Tailscale. "
                "Full Claude with persistent sessions and Mac filesystem access. "
                "Claude can write/review scripts, research topics, debug code, analyse data, "
                "draft text, and reason through complex problems. "
                "Returns Claude's response plus a session_id, pass session_id back as "
                "resume_session_id to continue the same conversation across multiple calls."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task or question for Claude. Be specific."
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional extra context: code, error messages, background info."
                    },
                    "resume_session_id": {
                        "type": "string",
                        "description": (
                            "Optional. Session ID returned by a previous ask_claude call. "
                            "Pass this to continue the same conversation."
                        )
                    }
                },
                "required": ["task"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name != "ask_claude":
        raise ValueError(f"Unknown tool: {name}")

    if not MAC_HOST:
        return [types.TextContent(type="text",
            text="Error: CLAUDE_BRIDGE_SSH_HOST env var not set. See README.")]

    task           = arguments.get("task", "").strip()
    context        = arguments.get("context", "").strip()
    resume_session = arguments.get("resume_session_id", "").strip()

    if not task:
        return [types.TextContent(type="text", text="Error: `task` cannot be empty.")]

    call_id    = uuid.uuid4().hex[:8]
    started_at = time.monotonic()
    ts         = datetime.now(timezone.utc).isoformat()

    log_event({
        "event":      "start",
        "id":         call_id,
        "ts":         ts,
        "task":       task,
        "context":    context or None,
        "session_id": resume_session or None,
    })

    prompt = f"Context:\n{context}\n\n---\n\nTask:\n{task}" if context else task

    claude_args = [MAC_CLAUDE, "-p", '"$TASK"',
                   "--output-format", "json",
                   "--dangerously-skip-permissions"]
    if resume_session:
        claude_args += ["--resume", shlex.quote(resume_session)]

    # Wrap with remote timeout so Claude is killed on the Mac if SSH drops.
    # Uses portable shell: 'timeout' (Linux/GNU) or 'gtimeout' (macOS + coreutils).
    # Falls back to no timeout if neither is available.
    _remote_timeout = max(CLAUDE_TIMEOUT - 10, 30)
    _timeout_prefix = (
        f'_T=$(command -v timeout 2>/dev/null || command -v gtimeout 2>/dev/null || true); '
        f'${{_T:+$_T {_remote_timeout}}}'
    )
    remote_cmd = f'TASK=$(cat); {_timeout_prefix} {" ".join(claude_args)}'

    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", *SSH_OPTS, MAC_HOST, remote_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()), timeout=CLAUDE_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            duration = round(time.monotonic() - started_at, 1)
            log_event({"event": "timeout", "id": call_id,
                        "ts": datetime.now(timezone.utc).isoformat(), "duration": duration})
            return [types.TextContent(type="text",
                text=f"Claude timed out after {CLAUDE_TIMEOUT}s.")]

        duration = round(time.monotonic() - started_at, 1)

        if proc.returncode != 0:
            err = stderr.decode().strip() or f"exit code {proc.returncode}"
            log_event({"event": "error", "id": call_id,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "duration": duration, "error": err})
            return [types.TextContent(type="text", text=f"Claude error: {err}")]

        raw = stdout.decode().strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log_event({"event": "done", "id": call_id,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "duration": duration, "response": raw})
            return [types.TextContent(type="text", text=raw)]

        result     = data.get("result", "(no result)")
        session_id = data.get("session_id", "")
        cost       = data.get("total_cost_usd", 0)
        is_error   = data.get("is_error", False)

        log_event({
            "event":      "done",
            "id":         call_id,
            "ts":         datetime.now(timezone.utc).isoformat(),
            "duration":   duration,
            "session_id": session_id,
            "cost_usd":   cost,
            "response":   result,
        })

        if is_error:
            return [types.TextContent(type="text", text=f"Claude error: {result}")]

        footer = f"\n\n---\n session_id: `{session_id}`  ${cost:.4f}  {duration}s"
        return [types.TextContent(type="text", text=result + footer)]

    except Exception as exc:
        duration = round(time.monotonic() - started_at, 1)
        err = f"Bridge error ({type(exc).__name__}): {exc}"
        log_event({"event": "error", "id": call_id,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "duration": duration, "error": err})
        return [types.TextContent(type="text", text=err)]


async def main():
    if MAC_HOST:
        asyncio.ensure_future(_log_claude_version())  # non-blocking: do not delay stdio ready
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
