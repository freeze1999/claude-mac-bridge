# claude-mac-bridge

MCP server that lets an AI agent delegate tasks to **Claude Code running on a
remote Mac** over SSH or Tailscale. It returns structured output with a reusable
session ID.

## How it works

```text
Agent → ask_claude → SSH → Claude Code on the Mac → response + session_id
```

The bridge runs Claude Code non-interactively with `claude -p`, parses its JSON
response, and records each delegation in a local audit log.

## When this is useful

Use the bridge when your main agent or UI runs on another machine, but the code,
toolchain, and authenticated Claude Code installation live on your Mac. One MCP
call moves the coding task into that environment without manual SSH commands,
prompt escaping, JSON parsing, or session bookkeeping.

Pass the returned ID back as `resume_session_id` for related work. Claude
resumes the saved conversation, decisions, and task context while the worktree
stays on the Mac.

## Requirements

- [Claude Code](https://code.claude.com/docs/en/quickstart) installed and signed in on the Mac
- Passwordless SSH from the agent machine to the Mac
- Python 3.10+ on the agent machine
- Tailscale on both machines, if the Mac is reached through Tailscale
- GNU `timeout` on the Mac for remote process cleanup (`brew install coreutils`)

## Setup

### 1. Prepare Claude Code on the Mac

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude  # follow the sign-in prompt
claude -p "what is 2+2" --output-format json
```

### 2. Prepare SSH on the agent machine

```bash
ssh-keygen -t ed25519 -f ~/.ssh/mac_bridge
ssh-copy-id -i ~/.ssh/mac_bridge.pub user@<mac-tailscale-ip>
ssh -i ~/.ssh/mac_bridge user@<mac-tailscale-ip> "claude --version"
```

### 3. Connect your agent

```bash
git clone https://github.com/freeze1999/claude-mac-bridge.git
cd claude-mac-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

BRIDGE_DIR="$PWD"
codex mcp add claude_bridge \
  --env CLAUDE_BRIDGE_SSH_HOST="user@100.x.x.x" \
  --env CLAUDE_BRIDGE_CLAUDE_BIN="/opt/homebrew/bin/claude" \
  -- "$BRIDGE_DIR/.venv/bin/python" "$BRIDGE_DIR/server.py"
```

Run `codex mcp list`, restart Codex, then use `/mcp` to confirm the server is
active. The agent discovers `ask_claude` from the server automatically. For
calls longer than Codex's default MCP tool timeout, add
`tool_timeout_sec = 620` under `[mcp_servers.claude_bridge]` in
`~/.codex/config.toml`.

Claude Code can register the same STDIO server with its own MCP command:

```bash
BRIDGE_DIR="/absolute/path/to/claude-mac-bridge"
claude mcp add --transport stdio --scope user claude_bridge \
  -e CLAUDE_BRIDGE_SSH_HOST="user@100.x.x.x" \
  -e CLAUDE_BRIDGE_CLAUDE_BIN="/opt/homebrew/bin/claude" \
  -- "$BRIDGE_DIR/.venv/bin/python" "$BRIDGE_DIR/server.py"
```

## Tool usage

```python
# Run in a specific project on the Mac
r1 = ask_claude(
    task="Fix the failing tests",
    workdir="/Users/me/projects/app",
)

# Continue the same Claude conversation
r2 = ask_claude(
    task="Now simplify the implementation",
    workdir="/Users/me/projects/app",
    resume_session_id="<session_id from r1>",
)
```

`ask_claude` accepts:

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task` | yes | Task or question for Claude |
| `context` | no | Extra code, errors, or background |
| `workdir` | no | Absolute working directory on the Mac |
| `resume_session_id` | no | Session ID returned by an earlier call |

## Configuration

| Environment variable | Default | Description |
|----------------------|---------|-------------|
| `CLAUDE_BRIDGE_SSH_HOST` | required | SSH target, such as `user@100.x.x.x` |
| `CLAUDE_BRIDGE_CLAUDE_BIN` | `claude` | Claude Code binary on the Mac |
| `CLAUDE_BRIDGE_TIMEOUT` | `600` | Delegation timeout in seconds |
| `CLAUDE_BRIDGE_LOG_PATH` | `./bridge.log` | Audit log path |
| `CLAUDE_BRIDGE_LOG_MAX_BYTES` | `10000000` | Log rotation threshold |
| `CLAUDE_BRIDGE_SKIP_PERMISSIONS` | `false` | Pass `--dangerously-skip-permissions` to Claude Code |

## Monitoring

```bash
python3 monitor.py
python3 monitor.py /path/to/bridge.log
```

## Trust boundary

Claude Code's permission configuration controls what delegated tasks may do on
the Mac. Setting `CLAUDE_BRIDGE_SKIP_PERMISSIONS=true` removes that layer for
every caller of this bridge. The audit log contains complete task and response
text and is gitignored by default.

## Status

The bridge supports remote working directories, structured Claude output,
session recovery, concurrent calls, audit logging, and log rotation.

Argument building, JSON parsing, working-directory handling, and log rotation
are unit-tested. New and resumed Claude sessions are also exercised with live
CLI runs.

## License

MIT.
