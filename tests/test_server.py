"""Tests for the pure decision logic: argument building, the remote command
wrapper, result parsing, and log rotation. The SSH path is deliberately
untested here; it is exercised by a supervised live delegation."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import (
    build_claude_args,
    build_remote_command,
    parse_result,
    should_rotate,
)
import server


def test_args_default_has_no_skip_permissions():
    args = build_claude_args("claude", "", skip_permissions=False)
    assert "--dangerously-skip-permissions" not in args
    assert args[:3] == ["claude", "-p", '"$TASK"']


def test_args_skip_permissions_opt_in():
    args = build_claude_args("claude", "", skip_permissions=True)
    assert "--dangerously-skip-permissions" in args


def test_args_resume_is_quoted():
    args = build_claude_args("claude", "abc; rm -rf /", skip_permissions=False)
    i = args.index("--resume")
    assert args[i + 1] == "'abc; rm -rf /'"


def test_claude_binary_is_quoted():
    args = build_claude_args("/Applications/Claude Code/claude", "",
                             skip_permissions=False)
    assert args[0] == "'/Applications/Claude Code/claude'"


def test_task_placeholder_is_literal():
    # Load-bearing: the task goes over stdin, never the command line.
    args = build_claude_args("claude", "", skip_permissions=False)
    assert '"$TASK"' in args


def test_remote_command_reads_stdin_and_has_timeout():
    cmd = build_remote_command(["claude", "-p", '"$TASK"'], 600)
    assert cmd.startswith("TASK=$(cat); ")
    assert "590" in cmd            # 10s under the local budget
    assert "gtimeout" in cmd       # macOS fallback present


def test_remote_timeout_floor():
    cmd = build_remote_command(["x"], 20)
    assert "30" in cmd             # never below 30s


def test_remote_command_changes_to_quoted_workdir():
    cmd = build_remote_command(["claude", "-p", '"$TASK"'], 600,
                               "/Users/me/my repo")
    assert cmd.startswith("TASK=$(cat); cd '/Users/me/my repo' || exit $?; ")


def test_call_rejects_relative_workdir_before_ssh():
    original_host = server.MAC_HOST
    server.MAC_HOST = "configured-host"
    try:
        result = asyncio.run(server.call_tool(
            "ask_claude", {"task": "test", "workdir": "relative/path"}
        ))
    finally:
        server.MAC_HOST = original_host
    assert "must be an absolute path" in result[0].text


def test_parse_result_structured():
    raw = ('{"result": "done", "session_id": "s1", '
           '"total_cost_usd": 0.12, "is_error": false}')
    p = parse_result(raw)
    assert p == {"result": "done", "session_id": "s1", "cost": 0.12,
                 "is_error": False, "structured": True}


def test_parse_result_error_flag():
    assert parse_result('{"result": "boom", "is_error": true}')["is_error"]


def test_parse_result_non_json_falls_back():
    p = parse_result("plain text crash output")
    assert p["structured"] is False
    assert p["result"] == "plain text crash output"


def test_should_rotate(tmp_path):
    f = tmp_path / "log"
    assert should_rotate(str(f), 10) is False          # missing file
    f.write_text("x" * 5)
    assert should_rotate(str(f), 10) is False
    f.write_text("x" * 10)
    assert should_rotate(str(f), 10) is True
    assert should_rotate(str(f), 0) is False           # 0 disables rotation
