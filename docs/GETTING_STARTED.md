# Getting Started with sandclaude

Two ways to use sandclaude:

1. **Terminal + REST API** — deploy on a cloud server, submit tasks via curl
2. **MCP + Claude Code** — use sandclaude tools directly inside Claude Code

Both require a running sandclaude server. This guide covers everything from zero to working PRs.

---

## Prerequisites

- An Anthropic API key (`sk-ant-...`)
- Docker installed on the target machine
- Python 3.10+ (for MCP plugin, on your local machine)

---

## Option 1: Deploy on AWS EC2 + Use via Terminal

### Step 1: Launch an EC2 Instance

- **AMI:** Ubuntu 22.04 or 24.04
- **Instance type:** `t3.medium` or larger (2 vCPU, 4 GB RAM minimum)
- **Storage:** 20 GB+
- **Security group inbound rules:**
  - Port 22 (TCP) — SSH from your IP
  - Port 3271 (TCP) — sandclaude API from your IP (or use SSH tunnel instead)

SSH in:
```bash
ssh -i "your-key.pem" ubuntu@your-ec2-public-ip
```

### Step 2: Install Docker

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker ubuntu
newgrp docker
```

### Step 3: Clone and Configure sandclaude

```bash
git clone https://github.com/kosaki-kamui/sandclaude.git
cd sandclaude
cp .env.example .env
nano .env
```

If you already use GitHub SSH keys and want to avoid repeated HTTPS credential prompts:

```bash
git clone git@github.com:kosaki-kamui/sandclaude.git
```

Or configure Git once to auto-rewrite GitHub HTTPS URLs to SSH:

```bash
git config --global url."git@github.com:".insteadOf "https://github.com/"
```

Set at minimum:
```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

Optional but recommended for private repos:
```
GIT_TOKEN=github_pat_your-token-here
ALLOWED_DOMAINS=registry.npmjs.org,pypi.org,files.pythonhosted.org
```

### Step 4: Build and Start

```bash
# Build the runner image (runs tasks inside containers)
docker build -t sandclaude-runner -f Dockerfile.runner .

# Start the API server + supporting services
docker compose up -d --build

# Verify it's running
curl http://localhost:3271/health
# Should return: {"status":"ok","version":"0.4.0"}
```

> **Note:** The API container automatically fixes `./data` bind-mount ownership
> at startup, so no manual `chown` is needed.

### Step 5: Get Your Auth Token

```bash
cat data/.token
```

Save this — you'll need it for all API calls.

### Step 6: Submit a Task

From your **local machine** (or from the EC2 instance):

```bash
TOKEN=your-token-here
HOST=http://your-ec2-public-ip:3271

# Submit a task against a public repo
curl -s -X POST $HOST/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "https://github.com/your-user/your-repo.git",
    "prompt": "Find and fix all security bugs in server.py",
    "max_turns": 15
  }' | python3 -m json.tool
```

For a **local repo on the EC2 host**:
```bash
curl -s -X POST $HOST/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "/home/ubuntu/sandclaude/demo/demo-repo",
    "prompt": "Add type hints to all functions",
    "max_turns": 10
  }' | python3 -m json.tool
```

### Step 7: Check Status

```bash
# List all tasks
curl -s -H "Authorization: Bearer $TOKEN" $HOST/tasks | python3 -m json.tool

# Get a specific task's full result (diff + audit)
curl -s -H "Authorization: Bearer $TOKEN" $HOST/tasks/task-XXXXX | python3 -m json.tool

# Get just the diff
curl -s -H "Authorization: Bearer $TOKEN" $HOST/tasks/task-XXXXX/diff
```

### Step 8: Create a PR (Optional)

Requires the [`gh` CLI](https://cli.github.com) installed on the server and `GIT_TOKEN` with write access (Contents: Read and write, Pull requests: Read and write). The `gh` CLI is pre-installed in the Docker Compose setup; bare-metal deployments must install it separately.

```bash
curl -s -X POST $HOST/tasks/task-XXXXX/create-pr \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title": "Fix security bugs in server.py"}' | python3 -m json.tool
```

### Step 9: Other Useful Commands

```bash
# Cancel a running task
curl -s -X POST -H "Authorization: Bearer $TOKEN" $HOST/tasks/task-XXXXX/cancel

# Delete a completed task
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" $HOST/tasks/task-XXXXX

# Check pool stats (active/queued/max)
curl -s -H "Authorization: Bearer $TOKEN" $HOST/pool | python3 -m json.tool

# View audit trail
curl -s -H "Authorization: Bearer $TOKEN" $HOST/tasks/task-XXXXX/audit | python3 -m json.tool

# View full transcript (every agent action)
curl -s -H "Authorization: Bearer $TOKEN" $HOST/tasks/task-XXXXX/transcript | python3 -m json.tool
```

### Updating sandclaude

When there's a new version:
```bash
cd ~/sandclaude
git pull
docker build -t sandclaude-runner -f Dockerfile.runner .
docker compose down && docker compose up -d --build
```

---

## Option 2: Use via MCP Plugin in Claude Code

The MCP plugin lets you submit and manage sandclaude tasks directly from Claude Code using natural language.

### Step 1: Deploy the Server

Follow Steps 1-5 from Option 1 above. You need a running sandclaude server.

### Step 2: Set Up SSH Tunnel

The MCP plugin refuses to send bearer tokens over non-local HTTP (security). Use an SSH tunnel to securely forward the port:

```bash
# Run this in a separate terminal — keep it open
ssh -i "your-key.pem" -L 3271:localhost:3271 ubuntu@your-ec2-public-ip -N
```

This makes `localhost:3271` on your Mac forward to the EC2 server. Verify:
```bash
curl http://localhost:3271/health
```

### Step 3: Get the Auth Token

```bash
# From EC2
ssh -i "your-key.pem" ubuntu@your-ec2-public-ip 'cat ~/sandclaude/data/.token'
```

### Step 4: Install sandclaude Locally

On your **local machine** (needed for the MCP plugin Python process):
```bash
cd /path/to/sandclaude
pip install .
```

### Step 5: Register the MCP Server

```bash
claude mcp add --transport stdio sandclaude -s user \
  --env sandclaude_URL=http://localhost:3271 \
  --env sandclaude_TOKEN=YOUR_TOKEN_HERE \
  -- python3 -m sandclaude.mcp_plugin
```

The `-s user` flag makes it available across all projects.

**Important:** Use the actual token string, not a shell variable like `$SC_TOKEN`. The `claude mcp add` command stores the literal value — shell variables are not expanded and will be saved as empty strings.

### Step 6: Verify Connection

```bash
claude mcp list
```

You should see `sandclaude: python3 -m sandclaude.mcp_plugin - Connected`.

If it shows "Failed to connect":
- Make sure the SSH tunnel is running in a separate terminal
- Verify `curl http://localhost:3271/health` returns `{"status":"ok"}`
- Check the token is correct: `claude mcp remove sandclaude -s user` and re-add with the right value
- The URL must be `http://localhost:...` — non-local HTTP URLs are blocked for security

You can also verify the MCP plugin starts manually:
```bash
sandclaude_URL=http://localhost:3271 sandclaude_TOKEN=YOUR_TOKEN python3 -m sandclaude.mcp_plugin
```
If it hangs silently, it's working (waiting for stdio). Press Ctrl+C to exit. If it prints an error, fix that first.

### Step 7: Verify in Claude Code

Start a **new** Claude Code session (MCP servers are loaded at startup):

```bash
claude
```

Type `/mcp` to check connected MCP servers. sandclaude should appear in the list.

### Step 8: Use in Claude Code

Use natural language in your Claude Code session:

**Submit a task:**
> Use cloud_submit to submit a sandclaude task. Repo: https://github.com/user/repo.git, prompt: "Fix all security bugs in server.py and add tests", max_turns: 15

**Check status:**
> Check my sandclaude task status

**Get result:**
> Show me the result of task-XXXXX

**Create a PR:**
> Create a PR from task-XXXXX

**Cancel a task:**
> Cancel task-XXXXX

**Delete a task:**
> Delete task-XXXXX

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `cloud_submit` | Submit a coding task (repo, prompt, model, max_turns, allowed_domains) |
| `cloud_status` | List all your tasks with status and cost |
| `cloud_result` | Get diff + audit log for a completed task |
| `cloud_cancel` | Cancel a running or queued task |
| `cloud_create_pr` | Create a GitHub PR from a completed task's diff |
| `cloud_delete` | Delete a completed/failed/cancelled task |

---

## Budget Control Walkthrough

sandclaude can estimate task cost **before execution** and block tasks that would exceed a budget cap. This section walks through the full workflow — from setting a budget to handling each possible outcome.

### How budget estimation works

When a task has a budget cap (either from `cost_budget_usd` on the task or `max_cost_usd` on a preset), sandclaude runs a pre-flight cost estimate before the agent starts. The estimator uses model pricing, `max_turns`, prompt length, and any addon costs (AI review, PR title/summary). The estimate intentionally errs on the conservative side — actual cost is usually lower.

The effective budget cap is always `min(preset.max_cost_usd, task.cost_budget_usd)`. A task can never raise the budget above the preset ceiling.

### Admin: create a budget-controlled preset

An admin creates a preset that controls how much any task using it can cost:

```bash
TOKEN=$(cat data/.token)
HOST=http://localhost:3271

# Strict budget: reject tasks predicted to cost more than $3
curl -s -X PUT $HOST/policies/team-standard \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "max_cost_usd": 3.00,
    "budget_fail_policy": "reject",
    "max_turns": 25,
    "requires_approval_for": ["create_pr"]
  }' | jq .

# Approval-gated budget: expensive tasks need human sign-off
curl -s -X PUT $HOST/policies/team-reviewed \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "max_cost_usd": 5.00,
    "budget_fail_policy": "require_approval",
    "max_turns": 50,
    "requires_approval_for": ["create_pr"]
  }' | jq .
```

The three `budget_fail_policy` values:
- **`reject`** (default) — task is rejected outright (HTTP 422), never created
- **`warn`** — task proceeds with a warning in the response; no blocking
- **`require_approval`** — task is created but blocked in `pending_approval` until a human approves or rejects the budget gate

### User: submit a task with a budget

A developer submits a task with a per-task budget. If the task also uses a preset, the effective cap is the lower of the two.

```bash
DEV_TOKEN=your-developer-token
HOST=http://localhost:3271

# Task with a per-task budget (no preset)
curl -s -X POST $HOST/tasks \
  -H "Authorization: Bearer $DEV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "https://github.com/your-org/your-repo.git",
    "prompt": "Add input validation to the /users endpoint and write tests",
    "cost_budget_usd": 2.00,
    "max_turns": 15
  }' | jq .
```

The response includes a `budget_check` field showing the estimation result:

```json
{
  "id": "task-a1b2c3d4",
  "status": "queued",
  "budget_check": {
    "status": "passed",
    "predicted_total_usd": 0.4860,
    "max_budget_usd": 2.0,
    "confidence": "high",
    "mode": "static"
  }
}
```

The `budget_check.status` tells you what happened:

| `status` | Meaning | What to do |
|----------|---------|------------|
| `passed` | Predicted cost is under budget | Nothing — task is running |
| `warning` | Predicted cost exceeds budget, but policy says proceed | Review cost after completion |
| `requires_approval` | Task is blocked until a human approves | Approve or reject the budget gate (see below) |
| `rejected` | Task was not created (HTTP 422) | Lower `max_turns`, use a cheaper model, or increase the budget |

### Outcome: `passed`

The task proceeds normally. The `budget_check` is stored and visible in `GET /tasks/{id}`:

```bash
curl -s $HOST/tasks/task-a1b2c3d4 \
  -H "Authorization: Bearer $DEV_TOKEN" | jq .budget_check
```

### Outcome: `rejected` (HTTP 422)

The task is never created. The response body tells you why:

```bash
curl -s -X POST $HOST/tasks \
  -H "Authorization: Bearer $DEV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "https://github.com/your-org/your-repo.git",
    "prompt": "Refactor the entire codebase to use async/await",
    "cost_budget_usd": 0.50,
    "model": "claude-opus-4-6",
    "max_turns": 100
  }'
```

```json
{
  "detail": {
    "error": "predicted budget exceeds budget cap",
    "budget_check": {
      "status": "rejected",
      "predicted_total_usd": 54.0000,
      "max_budget_usd": 0.5,
      "confidence": "low",
      "message": "Predicted cost $54.0000 exceeds budget $0.50"
    }
  }
}
```

To unblock: reduce `max_turns`, switch to a cheaper model (e.g., `claude-sonnet-4-5` instead of `claude-opus-4-6`), increase `cost_budget_usd`, or simplify the prompt.

### Outcome: `requires_approval`

The task is created but blocked. No execution starts until a human approves.

```bash
# Submit against a preset with budget_fail_policy: require_approval
curl -s -X POST $HOST/tasks \
  -H "Authorization: Bearer $DEV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "https://github.com/your-org/your-repo.git",
    "prompt": "Migrate the database schema from v2 to v3",
    "policy_preset": "team-reviewed",
    "model": "claude-opus-4-6",
    "max_turns": 50
  }' | jq .
```

```json
{
  "id": "task-x9y8z7w6",
  "status": "pending_approval",
  "requires_approval": 1,
  "budget_check": {
    "status": "requires_approval",
    "predicted_total_usd": 27.0000,
    "max_budget_usd": 5.0,
    "message": "Predicted cost $27.0000 exceeds budget $5.00 — approval required"
  }
}
```

**Approve the budget gate** (requires `tasks:approve` scope):

```bash
curl -s -X POST $HOST/tasks/task-x9y8z7w6/approve/budget_exceeded \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "One-time migration, cost is acceptable"}' | jq .
# {"status": "approved", "execution": "resumed"}
```

The task transitions to `queued` and execution begins. Any post-execution gates (like `create_pr`) are created at this point.

**Reject the budget gate:**

```bash
curl -s -X POST $HOST/tasks/task-x9y8z7w6/reject/budget_exceeded \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Too expensive, break the task into smaller pieces"}' | jq .
# {"status": "rejected"}
```

The task is marked as `failed` with `error: "budget_approval_rejected"`. No execution ever starts.

### Budget gates in the approval UI

When a budget-gated task is created, you can also manage it through the browser-based approval UI:

1. **Generate an approval link:**
   ```bash
   curl -s -X POST $HOST/tasks/task-x9y8z7w6/approval-link/budget_exceeded \
     -H "Authorization: Bearer $TOKEN" | jq .approval_url
   ```
   This returns a signed URL (valid for 1 hour) that you can share via Slack or email.

2. **Open the link in a browser.** The approval page shows:
   - The task prompt, model, and repo/branch context
   - A **Budget Estimate** card with predicted cost, budget cap, confidence level, estimation mode, and the current gate decision
   - The current gate status — this updates live, so if someone has already approved or rejected the gate, the page reflects that

3. **Approve or reject** using your API token (the approval link grants view access only; the approve/reject action requires your own token entered in the UI).

The budget card on the approval page always shows the **live gate status** (pending, approved, or rejected), not the stale admission-time decision.

### Retry and budget control

When you retry a task with `POST /tasks/{id}/retry`, the new task goes through the same pre-flight budget check. The retry inherits `cost_budget_usd` and `policy_preset` from the original task, so the same budget cap applies:

```bash
# Original task completed but needs follow-up
curl -s -X POST $HOST/tasks/task-a1b2c3d4/retry \
  -H "Authorization: Bearer $DEV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Also add error handling for the edge cases"}' | jq .
```

If the retried task exceeds the budget, it is rejected or gated the same way as a fresh submission. There is no bypass.

### Checking budget status after creation

You can check a task's budget estimation at any time via `GET /tasks/{id}`:

```bash
curl -s $HOST/tasks/task-x9y8z7w6 \
  -H "Authorization: Bearer $TOKEN" | jq .budget_check
```

The response includes a `gate_status` field showing the current state of the budget gate:

```json
{
  "budget_check": {
    "status": "requires_approval",
    "predicted_total_usd": 27.0000,
    "max_budget_usd": 5.0,
    "gate_status": "approved"
  }
}
```

Here `status` is the original admission decision and `gate_status` is the current live state.

---

## Tips

- **Private repos:** Set `GIT_TOKEN` in `.env` on the server. For cloning, any HTTPS git host works (GitHub, GitLab, Bitbucket). For PR creation, only GitHub is supported (via `gh` CLI). Use a GitHub fine-grained PAT with Contents read/write + Pull requests read/write.
- **Dependencies:** Set `ALLOWED_DOMAINS=registry.npmjs.org,pypi.org,files.pythonhosted.org` so Claude can install npm/pip packages during execution.
- **Cost control:** Use `max_turns: 10` for simple tasks, `max_turns: 50` for complex ones. Each turn costs roughly $0.01-0.05 depending on the model.
- **Concurrent tasks:** Submit up to 3 tasks at once (configurable via `MAX_CONCURRENT`). Additional tasks are queued automatically.
- **Priority:** Set `"priority": "high"` to jump the queue for urgent tasks.
- **Webhooks:** Add `"notify": {"webhook": "https://hooks.slack.com/...", "on": ["completed", "failed"]}` to get Slack notifications with inline diff, cost, and audit summary.
- **Models:** Default is `claude-sonnet-4-5`. Use `"model": "claude-opus-4-6"` for harder tasks.

---

## Troubleshooting

### Task fails immediately with "403 Forbidden"
The Docker socket proxy is blocking the request. Check `docker compose logs api` for details. The docker-compose.yml should include `EXEC=1` in the socket-proxy environment. If you customized the file, ensure all required permissions are present.

### Task fails with "network not found"
The `sandclaude-agent-net` network wasn't created. Run `docker compose down && docker compose up -d` — Compose should create it automatically since the API service is attached to it.

### Task fails with "Not logged in" / 0 tokens used
The `ANTHROPIC_API_KEY` isn't reaching the Claude Agent SDK inside the container. Check that it's set in `.env` and not empty. Run `docker compose logs api` to see if the key is loaded.

### Task fails during setup with "authentication failed" (private repos)
Set `GIT_TOKEN` in `.env` with a valid GitHub PAT. For GitHub fine-grained tokens, you need at least Contents: Read access. Rebuild after adding: `docker compose down && docker compose up -d --build`.

### PR creation fails with "Author identity unknown"
Update to the latest sandclaude version — this was fixed. Run `git pull && docker compose down && docker compose up -d --build`.

### PR creation fails with authentication errors
Your `GIT_TOKEN` needs write access for PR creation: Contents: Read and write + Pull requests: Read and write. Update the token permissions on GitHub and restart.

### MCP shows "Failed to connect" in `claude mcp list`
1. Check SSH tunnel is running: `curl http://localhost:3271/health`
2. Check the token isn't empty: `cat ~/.claude.json | grep sandclaude_TOKEN`
3. If the token or URL is wrong, remove and re-add: `claude mcp remove sandclaude -s user`

### MCP connected but Claude Code doesn't use the tools
- Start a **new** session after registering the MCP server
- Be explicit: say "Use cloud_submit to submit a sandclaude task" rather than just "submit a task"
- Type `/mcp` in Claude Code to verify the connection

### Task completes with `error_max_turns` but has a diff
This is normal — the agent ran out of turns but completed the work. sandclaude treats this as `completed` (not `failed`) when a non-empty diff was produced. Increase `max_turns` if you want the agent to have more room.
