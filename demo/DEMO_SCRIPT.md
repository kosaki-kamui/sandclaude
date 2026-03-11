# sandclaude Demo Script

> Total time: ~2 minutes (excluding agent execution wait)

## Setup (before demo)
```bash
cp .env.example .env
# Add your ANTHROPIC_API_KEY to .env
sandclaude init
```

## Demo Flow

### 1. Show the buggy code (30 sec)
Open `demo/demo-repo/server.py` and point out the three bugs:
- **Line 13:** `eval()` with f-string - code injection vulnerability
- **Line 19:** No input validation - accepts any dict, no type checking
- **Line 31:** No null check, no try/except - crashes on missing user or network error

### 2. Submit the task (10 sec)
```bash
make dev-demo
```

### 3. Explain the architecture while waiting (60 sec)
- "The task is running in an isolated Docker container"
- "Setup phase: clones the repo - full internet access"
- "Agent phase: network switches - only api.anthropic.com + configured domains are reachable"
- "Claude decides what dependencies to install and installs them itself"
- "Every file read, every command run, every network request is logged"

### 4. Check progress (10 sec)
```bash
make dev-status
```

### 5. Show the results (20 sec)
```bash
make dev-result TASK_ID=task-abc12345
```

### 6. The "wow" moment (10 sec)
> "Every file the agent read. Every command it ran. Every network request, allowed or blocked. Full transparency. And it all runs in your VPC."
