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
  CLAUDE_BRIDGE_LOG_MAX_BYTES  Rotate the log past this size (default: 10000000)
  CLAUDE_BRIDGE_SKIP_PERMISSIONS  "true" to run Claude with
    --dangerously-skip-permissions. Default false: Claude Code's own
    permission config on the Mac applies. Setting this true means any agent
    that can call this MCP tool has arbitrary code execution on the Mac.
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
LOG_MAX_BYTES  = int(os.environ.get("CLAUDE_BRIDGE_LOG_MAX_BYTES", "10000000"))
SKIP_PERMISSIONS = os.environ.get(
    "CLAUDE_BRIDGE_SKIP_PERMISSIONS", "false").strip().lower() in ("1", "true", "yes")

app = Server("claude-bridge")


async def _log_claude_version():
    """Log the Claude Code CLI version at startup for debugging."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", *SSH_OPTS, MAC_HOST, f"{shlex.quote(MAC_CLAUDE)} --version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        version = out.decode().strip().splitlines()[0] if out else "unknown"
        log_event({"event": "startup", "claude_version": version,
                   "ts": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        log_event({"event": "startup", "claude_version": f"error: {e}",
                   "ts": datetime.now(timezone.utc).isoformat()})


def should_rotate(path: str, max_bytes: int) -> bool:
    try:
        return max_bytes > 0 and os.path.getsize(path) >= max_bytes
    except OSError:
        return False


def log_event(event: dict):
    """Append one JSON line; size-rotate to a single .1 generation first.
    The log holds full task/response text, rotation keeps it from growing
    without bound."""
    try:
        if should_rotate(LOG_FILE, LOG_MAX_BYTES):
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")
            f.flush()
    except Exception:
        pass


def build_claude_args(claude_bin: str, resume_session: str,
                      skip_permissions: bool) -> list[str]:
    """The literal '"$TASK"' is load-bearing: the task text is piped in over
    stdin (remote command starts with TASK=$(cat)) so it never touches the
    command line and cannot inject into the shell. Do not "fix" it into an
    f-string or shlex.quote of the prompt."""
    args = [shlex.quote(claude_bin), "-p", '"$TASK"', "--output-format", "json"]
    if skip_permissions:
        args.append("--dangerously-skip-permissions")
    if resume_session:
        args += ["--resume", shlex.quote(resume_session)]
    return args


def build_remote_command(args: list[str], timeout_s: int,
                         workdir: str = "") -> str:
    """Wrap with a remote timeout so Claude is killed on the Mac if SSH drops.
    Portable: 'timeout' (GNU) or 'gtimeout' (macOS + coreutils), or no
    timeout if neither exists. Remote budget is 10s under the local one so
    the remote side dies first."""
    remote_timeout = max(timeout_s - 10, 30)
    prefix = (
        '_T=$(command -v timeout 2>/dev/null || command -v gtimeout 2>/dev/null || true); '
        f'${{_T:+$_T {remote_timeout}}}'
    )
    change_dir = f"cd {shlex.quote(workdir)} || exit $?; " if workdir else ""
    return f'TASK=$(cat); {change_dir}{prefix} {" ".join(args)}'


def parse_result(raw: str) -> dict:
    """Parse `claude --output-format json` stdout. Falls back to the raw text
    when it is not JSON (older CLIs, crash output)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"result": raw, "session_id": "", "cost": 0.0,
                "is_error": False, "structured": False}
    return {"result": data.get("result", "(no result)"),
            "session_id": data.get("session_id", ""),
            "cost": data.get("total_cost_usd", 0),
            "is_error": data.get("is_error", False),
            "structured": True}


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
                    },
                    "workdir": {
                        "type": "string",
                        "description": (
                            "Optional absolute working directory on the remote Mac."
                        ),
                    },
                },
                "required": ["task"]
            }
        )
    ]


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate a timed-out SSH process, escalating to kill if needed."""
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


async def _run_ssh_command(remote_cmd: str, prompt: str,
                           timeout_s: int) -> tuple[int, bytes, bytes]:
    """Run one remote command and keep timeout cleanup out of tool dispatch."""
    proc = await asyncio.create_subprocess_exec(
        "ssh", *SSH_OPTS, MAC_HOST, remote_cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode()), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        await _stop_process(proc)
        raise
    return proc.returncode or 0, stdout, stderr


def _format_claude_response(raw: str, call_id: str,
                            duration: float) -> list[types.TextContent]:
    """Parse, log, and format a successful Claude process response."""
    parsed = parse_result(raw)
    if not parsed["structured"]:
        log_event({"event": "done", "id": call_id,
                   "ts": datetime.now(timezone.utc).isoformat(),
                   "duration": duration, "response": raw})
        return [types.TextContent(type="text", text=raw)]

    result = parsed["result"]
    session_id = parsed["session_id"]
    cost = parsed["cost"]
    log_event({
        "event":      "done",
        "id":         call_id,
        "ts":         datetime.now(timezone.utc).isoformat(),
        "duration":   duration,
        "session_id": session_id,
        "cost_usd":   cost,
        "response":   result,
    })

    if parsed["is_error"]:
        return [types.TextContent(type="text", text=f"Claude error: {result}")]

    footer = f"\n\n---\n session_id: `{session_id}`  ${cost:.4f}  {duration}s"
    return [types.TextContent(type="text", text=result + footer)]


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
    workdir        = arguments.get("workdir", "").strip()

    if not task:
        return [types.TextContent(type="text", text="Error: `task` cannot be empty.")]
    if workdir and not os.path.isabs(workdir):
        return [types.TextContent(type="text",
            text="Error: `workdir` must be an absolute path on the remote Mac.")]

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
        "workdir":    workdir or None,
    })

    prompt = f"Context:\n{context}\n\n---\n\nTask:\n{task}" if context else task

    claude_args = build_claude_args(MAC_CLAUDE, resume_session, SKIP_PERMISSIONS)
    remote_cmd  = build_remote_command(claude_args, CLAUDE_TIMEOUT, workdir)

    try:
        returncode, stdout, stderr = await _run_ssh_command(
            remote_cmd, prompt, CLAUDE_TIMEOUT
        )
        duration = round(time.monotonic() - started_at, 1)

        if returncode != 0:
            err = stderr.decode(errors="replace").strip() or f"exit code {returncode}"
            log_event({"event": "error", "id": call_id,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "duration": duration, "error": err})
            return [types.TextContent(type="text", text=f"Claude error: {err}")]

        raw = stdout.decode(errors="replace").strip()
        return _format_claude_response(raw, call_id, duration)

    except asyncio.TimeoutError:
        duration = round(time.monotonic() - started_at, 1)
        log_event({"event": "timeout", "id": call_id,
                   "ts": datetime.now(timezone.utc).isoformat(), "duration": duration})
        return [types.TextContent(type="text",
            text=f"Claude timed out after {CLAUDE_TIMEOUT}s.")]
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
