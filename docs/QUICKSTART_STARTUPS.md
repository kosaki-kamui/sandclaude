# Startup Quickstart: sandclaude in 30 Minutes

This guide gets a startup engineering team from zero to a working sandclaude deployment with policy presets, approval gates, and CI integration.

---

## What You'll Set Up

1. A running sandclaude server (Docker Compose)
2. Scoped API tokens for your team
3. A policy preset for bug-fix PRs (with approval required)
4. A sample task → approval → PR workflow
5. A GitHub Actions workflow for auto-fixing CI failures

**Time:** ~30 minutes

---

## Step 1: Deploy sandclaude (5 min)

```bash
git clone https://github.com/kosaki-kamui/sandclaude.git
cd sandclaude
cp .env.example .env

# Edit .env — set at minimum:
#   ANTHROPIC_API_KEY=sk-ant-...
#   GIT_TOKEN=github_pat_...  (for PR creation)

# Build and start
docker build -t sandclaude-runner -f Dockerfile.runner .
docker compose up -d --build

# Verify
curl http://localhost:3271/health
# {"status":"ok","version":"0.2.5"}

# Get your admin token
cat data/.token
```

## Step 2: Create Team Tokens (5 min)

The admin token (from `data/.token`) has full access. Create scoped tokens for your team:

```bash
TOKEN=$(cat data/.token)
HOST=http://localhost:3271

# Create a CI bot token (can create tasks and PRs, but not manage policies)
curl -s -X POST $HOST/tokens \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ci-bot",
    "scopes": ["tasks:create", "tasks:read", "prs:create"],
    "expires_in_days": 90
  }' | jq .

# IMPORTANT: Save the "token" field — it's shown only once!

# Create a developer token (can create tasks and approve PRs)
curl -s -X POST $HOST/tokens \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "dev-alice",
    "scopes": ["tasks:create", "tasks:read", "tasks:approve", "prs:create"],
    "expires_in_days": 30
  }' | jq .

# List all tokens
curl -s $HOST/tokens -H "Authorization: Bearer $TOKEN" | jq .
```

## Step 3: Review Built-in Presets (2 min)

sandclaude ships with 5 built-in policy presets:

```bash
curl -s $HOST/policies -H "Authorization: Bearer $TOKEN" | jq '.[].name'
# "bugfix-pr"
# "deps-upgrade"
# "docs-only"
# "review-only"
# "tests-only"

# Inspect the bugfix-pr preset
curl -s $HOST/policies/bugfix-pr -H "Authorization: Bearer $TOKEN" | jq .config
```

The `bugfix-pr` preset:
- Allows all commands and write paths
- Allows package registry domains
- **Requires approval before PR creation**
- Caps cost at $5.00 per task

## Step 4: Submit a Task with Approval (5 min)

```bash
# Submit a bug-fix task using the bugfix-pr preset
TASK=$(curl -s -X POST $HOST/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "https://github.com/your-org/your-repo.git",
    "prompt": "Fix the authentication bypass in the login endpoint. Add a test.",
    "policy_preset": "bugfix-pr",
    "max_turns": 20
  }')
echo $TASK | jq .
TASK_ID=$(echo $TASK | jq -r .id)

# Wait for completion...
curl -s $HOST/tasks/$TASK_ID -H "Authorization: Bearer $TOKEN" | jq .status

# Once completed, check the risk summary
curl -s $HOST/tasks/$TASK_ID/risk -H "Authorization: Bearer $TOKEN" | jq .

# Check approval gates — create_pr should be pending
curl -s $HOST/tasks/$TASK_ID/approvals -H "Authorization: Bearer $TOKEN" | jq .

# Try to create PR — will be blocked (409)
curl -s -X POST $HOST/tasks/$TASK_ID/create-pr \
  -H "Authorization: Bearer $TOKEN" | jq .
# {"detail": "PR creation requires approval..."}

# Review the diff
curl -s $HOST/tasks/$TASK_ID/diff -H "Authorization: Bearer $TOKEN"

# Approve and create PR
curl -s -X POST $HOST/tasks/$TASK_ID/approve/create_pr \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Reviewed diff, looks good"}' | jq .

# Now create the PR
curl -s -X POST $HOST/tasks/$TASK_ID/create-pr \
  -H "Authorization: Bearer $TOKEN" | jq .
```

## Step 5: Set Up CI Integration (10 min)

Copy the example workflow to your repo:

```bash
# In your project repo:
mkdir -p .github/workflows

# Option A: Auto-fix failing CI
cp /path/to/sandclaude/.github/workflows/sandclaude-fix-ci.yml \
   .github/workflows/sandclaude-fix-ci.yml

# Option B: Auto-review PRs
cp /path/to/sandclaude/.github/workflows/sandclaude-review-pr.yml \
   .github/workflows/sandclaude-review-pr.yml

# Option C: Weekly dependency upgrades
cp /path/to/sandclaude/.github/workflows/sandclaude-deps-upgrade.yml \
   .github/workflows/sandclaude-deps-upgrade.yml
```

Add repository secrets in GitHub Settings → Secrets:
- `SANDCLAUDE_URL`: Your server URL (e.g., `https://sandclaude.your-company.com`)
- `SANDCLAUDE_TOKEN`: The CI bot token from Step 2

## Step 6: Create a Custom Preset (optional, 3 min)

```bash
# Create a strict preset for production hotfixes
curl -s -X PUT $HOST/policies/hotfix \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "allowed_write_paths": ["src/", "lib/"],
    "allowed_domains": [],
    "allow_pr_creation": true,
    "requires_approval_for": ["create_pr"],
    "max_turns": 10,
    "max_cost_usd": 2.0
  }' | jq .

# Create a budget-controlled preset that requires approval for expensive tasks
curl -s -X PUT $HOST/policies/budget-gated \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "max_cost_usd": 5.00,
    "budget_fail_policy": "require_approval",
    "max_turns": 30,
    "requires_approval_for": ["create_pr"]
  }' | jq .
# Tasks predicted to cost >$5 will block in pending_approval.
# Tasks under budget proceed normally.
# The estimator intentionally errs on the safe side — actual cost may be lower.

# A developer submitting against this preset:
DEV_TOKEN=your-developer-token
curl -s -X POST $HOST/tasks \
  -H "Authorization: Bearer $DEV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "https://github.com/your-org/your-repo.git",
    "prompt": "Fix the login rate limiter",
    "policy_preset": "budget-gated",
    "max_turns": 20
  }' | jq .budget_check
# If under $5 → {"status": "passed", ...} — task runs immediately
# If over $5 → {"status": "requires_approval", ...} — task blocks
# Approve: POST /tasks/{id}/approve/budget_exceeded
# Reject:  POST /tasks/{id}/reject/budget_exceeded
```

For a complete end-to-end walkthrough (all four budget outcomes, approval UI,
retry behavior, and the admin/user split), see the
[Budget Control Walkthrough](GETTING_STARTED.md#budget-control-walkthrough)
in the Getting Started guide.

---

## What You Now Have

| Capability | How |
|-----------|-----|
| **Scoped team tokens** | `POST /tokens` with expiry and specific scopes |
| **Approval-gated PRs** | `bugfix-pr` preset requires approval before PR creation |
| **Risk assessment** | `GET /tasks/{id}/risk` shows what changed and where the risk is |
| **AI code review** | `POST /tasks/{id}/review` gets Claude's review of the diff |
| **CI auto-fix** | GitHub Actions workflow submits fix tasks on CI failure |
| **PR auto-review** | GitHub Actions posts AI review on new PRs |
| **Dep upgrades** | Weekly automated dependency upgrade PRs |
| **Custom policies** | `PUT /policies/{name}` to create team-specific presets |
| **Secrets management** | Tasks declare secrets, server resolves per policy |
| **Audit trail** | Every file, command, network request, and secret access logged |

---

## Next Steps

- **Slack notifications:** Add `"notify": {"webhook": "https://hooks.slack.com/..."}` to task submissions
- **MCP plugin:** Use sandclaude from inside Claude Code (see [Getting Started](GETTING_STARTED.md))
- **More presets:** Create presets for your team's common workflows
- **Token rotation:** Set `expires_in_days` and create new tokens before old ones expire
