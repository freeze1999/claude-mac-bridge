# claude-mac-bridge

MCP server that lets an AI agent delegate tasks to **Claude Code on a remote Mac** via SSH over Tailscale. Persistent sessions, 200k context, full Mac filesystem access.

```
Agent (server) → ask_claude tool → SSH → claude -p (Mac) → JSON response + session_id
```

## Why

Your agent runs on a Linux server. Claude Code runs on a Mac with Homebrew, Docker, Xcode, and 200k context. Sometimes you need both.

This bridge:
- **Gives your agent Mac access**: file system, tools, local services
- **Persists sessions**: pass `session_id` back to continue conversations across hours
- **Zero config on the Mac**: Claude Code runs headless via `-p`, no GUI needed
- **Full audit trail**: every delegation logged to `bridge.log` with cost + duration

## Quick start

```bash
# 1. Install
pip install mcp

# 2. Set up SSH key
ssh-keygen -t ed25519 -f ~/.ssh/mac_bridge
ssh-copy-id -i ~/.ssh/mac_bridge.pub user@<mac-tailscale-ip>

# 3. Set env vars
export CLAUDE_BRIDGE_SSH_HOST="user@100.x.x.x"
export CLAUDE_BRIDGE_CLAUDE_BIN="/opt/homebrew/bin/claude"

# 4. Run the server
python3 server.py

# 5. Wire into your MCP config (see Setup below)
```

## Requirements

- Claude Code on the Mac (`npm install -g @anthropic-ai/claude-code` or Homebrew)
- GNU `timeout` for remote process cleanup, install via `brew install coreutils` on macOS (provides `gtimeout`). Falls back gracefully if unavailable, but remote Claude processes may outlive timed-out SSH connections without it.
- Tailscale on both machines
- Passwordless SSH (key-based auth) from server → Mac
- Python 3.10+ on the server
- `mcp` package (`pip install mcp`)

## Setup

### 1. SSH key auth

```bash
# On server: generate a dedicated key
ssh-keygen -t ed25519 -f ~/.ssh/mac_bridge

# Copy to Mac
ssh-copy-id -i ~/.ssh/mac_bridge.pub user@<mac-tailscale-ip>

# Verify
ssh -i ~/.ssh/mac_bridge user@<mac-tailscale-ip> "echo ok"
```

### 2. Configure

| Env var | Required | Default | Description |
|---------|----------|---------|-------------|
| `CLAUDE_BRIDGE_SSH_HOST` | ✅ |, | SSH target: `user@100.x.x.x` |
| `CLAUDE_BRIDGE_CLAUDE_BIN` | ❌ | `claude` | Path to Claude binary on the Mac |
| `CLAUDE_BRIDGE_TIMEOUT` | ❌ | `600` | Max seconds per delegation (wall-clock, never pauses) |
| `CLAUDE_BRIDGE_LOG_PATH` | ❌ | `./bridge.log` | Path for the delegation audit log |
| `CLAUDE_BRIDGE_LOG_MAX_BYTES` | ❌ | `10000000` | Size-rotate the log (one `.1` generation kept) |
| `CLAUDE_BRIDGE_SKIP_PERMISSIONS` | ❌ | `false` | Run Claude with `--dangerously-skip-permissions` (read the trust boundary below first) |

### 3. Wire into MCP config

```yaml
mcp_servers:
  claude_bridge:
    command: "python3"
    args: ["/path/to/server.py"]
    env:
      CLAUDE_BRIDGE_SSH_HOST: "user@100.x.x.x"
      CLAUDE_BRIDGE_CLAUDE_BIN: "/opt/homebrew/bin/claude"
    timeout: 620  # must exceed CLAUDE_BRIDGE_TIMEOUT
```

## API

### `ask_claude(task, context?, resume_session_id?)`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task` | ✅ | The task or question. Be specific. |
| `context` | ❌ | Extra context: code, error messages, background. |
| `resume_session_id` | ❌ | From a previous call, continues that conversation. |

**Returns:** Claude's response with `session_id`, cost, and duration appended.

```python
# Basic
result = ask_claude(task="Write a Python script that reads a CSV and...")

# With context
result = ask_claude(
    task="Find the bug",
    context="def foo(): ...\nError: TypeError..."
)

# Persistent session
r1 = ask_claude(task="Review this PR")
r2 = ask_claude(
    task="Fix the first issue you found",
    resume_session_id="<session_id from r1>"
)
```

## Session chaining

Every response includes a `session_id`. Pass it back as `resume_session_id` to continue the same conversation, Claude remembers all prior context, files read, and decisions made.

**Rule: always chain session_ids for related tasks.** Without `resume_session_id`, each call is a cold start.

```python
r1 = ask_claude(task="Build the auth module")
# r1 contains: session_id = "abc123"

r2 = ask_claude(task="Add rate limiting to the auth module", resume_session_id="abc123")
r3 = ask_claude(task="Write tests for all of it", resume_session_id="abc123")
```

## Long tasks: the tmux blocking loop

The MCP tool has a hard wall-clock timeout (default 600s). For tasks that may run longer, project scaffolding, npm installs, multi-file builds, use this tmux pattern instead. It blocks until Claude finishes, auto-handles confirmation prompts, and returns the full result + `session_id`.

```bash
#!/usr/bin/env bash
MAC="user@<mac-tailscale-ip>"
TMUX="/opt/homebrew/bin/tmux"
CLAUDE="/opt/homebrew/bin/claude"
SESSION="agent-work"
MAX_WAIT=3600   # 60 min ceiling
POLL=45

# 1. write task to file (avoids SSH quoting hell)
scp /tmp/task.md "$MAC:~/task.md"

# 2. start fresh tmux session
ssh "$MAC" "$TMUX kill-session -t $SESSION 2>/dev/null; \
  $TMUX new-session -d -s $SESSION -x 220 -y 50"

# 3. launch claude
ssh "$MAC" "$TMUX send-keys -t $SESSION \
  '$CLAUDE -p "Read ~/task.md and execute every step." \
  --output-format json --dangerously-skip-permissions 2>&1 | tee ~/task_output.json' Enter"

# 4. blocking poll -- auto-answer confirmations, exit on shell prompt
elapsed=0
while [ $elapsed -lt $MAX_WAIT ]; do
  sleep $POLL
  elapsed=$((elapsed + POLL))
  pane=$(ssh "$MAC" "$TMUX capture-pane -t $SESSION -p 2>/dev/null | tail -10")

  # auto-approve confirmation prompts
  if echo "$pane" | grep -qiE 'do you want|allow|yes.*no|\[y/n\]|approve|1\).*2\)'; then
    ssh "$MAC" "$TMUX send-keys -t $SESSION '2' Enter"
    sleep 3; continue
  fi

  # shell prompt = claude exited
  echo "$pane" | grep -qE '^\s*(%|\$|>)\s*$' && break
done

# 5. grab result + session_id
result=$(ssh "$MAC" "cat ~/task_output.json 2>/dev/null")
session_id=$(echo "$result" | python3 -c \
  "import json,sys; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)

# 6. cleanup
ssh "$MAC" "$TMUX kill-session -t $SESSION 2>/dev/null; rm -f ~/task.md ~/task_output.json"
echo "done in ${elapsed}s | session_id: $session_id"
```

**When to use tmux vs MCP tool:**

| Task type | Use |
|-----------|-----|
| Quick question, code review, research | MCP tool |
| Multi-step work with session chaining | MCP tool + `resume_session_id` |
| Project scaffolding / `npm install` | tmux blocking loop |
| Multi-file writes across a codebase | tmux blocking loop |
| Unknown duration / likely >5 min | tmux blocking loop |

**If unsure → tmux.** A 30s task in tmux costs nothing extra. A 12-min task in the MCP tool gets killed at 10 min.


## Monitoring

```bash
python3 monitor.py                    # live feed (tails bridge.log)
python3 monitor.py /path/to/bridge.log
```

Color-coded live TUI showing start/done/error/timeout events with task previews, response length, cost, and duration.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Host key verification failed` | Mac host key unknown | `ssh -o StrictHostKeyChecking=accept-new user@<ip>` once |
| `Permission denied (publickey)` | SSH key not deployed | Run `ssh-copy-id`, verify with `ssh -i ~/.ssh/mac_bridge user@<ip>` |
| `claude: command not found` | Wrong binary path | Set `CLAUDE_BRIDGE_CLAUDE_BIN` to full path (`which claude` on Mac) |
| Bridge times out | Task too long for `CLAUDE_BRIDGE_TIMEOUT` | Increase timeout via env var, or use the tmux pattern for tasks >10 min (see the long tasks section) |
| `is_error: true` with no detail | Claude returned an error | Check `bridge.log` for full response |
| Session not continuing | Claude process ended on Mac | Sessions live as long as the daemon on Mac, restarting Mac or Claude clears them |
| `TASK=$(cat)` errors with special chars | Prompt has unmatched quotes or shell metacharacters | The JSON output format should handle escaping, if hit, wrap task in a temp file |

## Security

- `bridge.log` contains full task/response history, **it is gitignored**, keep it local
- Never commit `.env` or SSH private keys
- `--dangerously-skip-permissions` is passed only when you set `CLAUDE_BRIDGE_SKIP_PERMISSIONS=true`, which removes Claude Code's own permission layer, only do that on a trusted Mac you control
- The SSH command runs as-is on your Mac, do not expose this bridge to untrusted clients

## Known limitations

- **Mac must be awake + on Tailscale.** If the Mac sleeps or goes offline, delegations timeout.
- **Session lifetime depends on Claude process.** If Claude restarts (Mac reboot, crash), session IDs are lost.
- **`bridge.log` grows unbounded.** No log rotation, monitor size or add your own.
- Async server, concurrent tool calls are supported but each delegation opens its own SSH connection to the Mac.
- **The permission bypass is per-server, not per-call.** `CLAUDE_BRIDGE_SKIP_PERMISSIONS` is read once at startup, so every delegation shares whatever the process began with.

## License

MIT.

## Trust boundary

By default the bridge runs Claude Code under the Mac's own permission
configuration. Setting `CLAUDE_BRIDGE_SKIP_PERMISSIONS=true` removes that
layer: any agent that can call this MCP tool then has arbitrary code
execution on the Mac. Only enable it if the MCP server is reachable
exclusively by agents you would hand a shell to.

The audit log (`bridge.log`) records the full task, context, and response
text of every delegation. Treat it as sensitive and leave it out of any
repo (it is gitignored here).

## Status

What this is: a single-tool MCP server that delegates tasks to Claude Code
on one Mac over SSH, with session continuity and an audit log.

What this is NOT: an authentication layer, a sandbox, or a multi-host
router. The argument building, output parsing, and log rotation are
unit-tested; the SSH path is exercised by supervised live delegations, not
CI. This file has a sibling repo, see PORTING.md.
