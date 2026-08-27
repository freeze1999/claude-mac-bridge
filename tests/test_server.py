"""Tests for the pure decision logic: argument building, the remote command
wrapper, result parsing, and log rotation. The SSH path is deliberately
untested here; it is exercised by a supervised live delegation."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import (
    build_claude_args,
    build_remote_command,
    parse_result,
    should_rotate,
)
import server


class FakeProcess:
    def __init__(self, delay=0):
        self.delay = delay
        self.returncode = 0
        self.input = None
        self.terminated = False

    async def communicate(self, input=None):
        self.input = input
        if self.delay:
            await asyncio.sleep(self.delay)
        return b"stdout", b"stderr"

    def terminate(self):
        self.terminated = True

    async def wait(self):
        return self.returncode


def test_run_ssh_command_returns_process_output(monkeypatch):
    proc = FakeProcess()

    async def create_process(*args, **kwargs):
        return proc

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", create_process)
    result = asyncio.run(server._run_ssh_command("remote", "prompt", 1))
    assert result == (0, b"stdout", b"stderr")
    assert proc.input == b"prompt"


def test_run_ssh_command_cleans_up_timeout(monkeypatch):
    proc = FakeProcess(delay=1)

    async def create_process(*args, **kwargs):
        return proc

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", create_process)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(server._run_ssh_command("remote", "prompt", 0.001))
    assert proc.terminated


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


def test_format_claude_response_structured(tmp_path):
    original_log = server.LOG_FILE
    server.LOG_FILE = str(tmp_path / "bridge.log")
    try:
        response = server._format_claude_response(
            '{"result":"done","session_id":"s1","total_cost_usd":0.12}',
            "call1", 1.5,
        )
    finally:
        server.LOG_FILE = original_log
    assert response[0].text.endswith("session_id: `s1`  $0.1200  1.5s")


def test_format_claude_response_error_and_raw(tmp_path):
    original_log = server.LOG_FILE
    server.LOG_FILE = str(tmp_path / "bridge.log")
    try:
        error = server._format_claude_response(
            '{"result":"boom","is_error":true}', "call1", 1.0
        )
        raw = server._format_claude_response("plain output", "call2", 2.0)
    finally:
        server.LOG_FILE = original_log
    assert error[0].text == "Claude error: boom"
    assert raw[0].text == "plain output"


def test_should_rotate(tmp_path):
    f = tmp_path / "log"
    assert should_rotate(str(f), 10) is False          # missing file
    f.write_text("x" * 5)
    assert should_rotate(str(f), 10) is False
    f.write_text("x" * 10)
    assert should_rotate(str(f), 10) is True
    assert should_rotate(str(f), 0) is False           # 0 disables rotation
