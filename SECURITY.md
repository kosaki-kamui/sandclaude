# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability in sandclaude, please report it responsibly:

1. **Do NOT open a public GitHub issue** for security vulnerabilities
2. Email: [create a private security advisory on GitHub](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
3. Include:
  - Description of the vulnerability
  - Steps to reproduce
  - Potential impact
  - Suggested fix (if any)

## Security Model

sandclaude provides **defense in depth with transparency**, not maximum isolation. See the [README](README.md#security-model) for the full threat model.

### What We Protect Against

| Threat | Mitigation |
|--------|------------|
| Code exfiltration via network | iptables rules in agent phase: `api.anthropic.com:443` + configured `allowed_domains` only |
| Credential exposure | API keys injected via env vars, never written to disk in containers |
| Unauthorized API access | Bearer token auth with timing-safe comparison |
| Cross-tenant task access | Task ownership enforced by bearer-token fingerprint |
| API-to-Docker privilege abuse | Compose uses Docker socket proxy (not direct socket mount in API service) |
| Orphaned containers | Automatic detection and cleanup on server restart |

### Known Limitations (Not Bugs)

These are documented architectural limitations, not vulnerabilities:

- **Docker isolation is not VM-level:** kernel exploits could escape the container
- **DNS restricted to Docker resolver (127.0.0.11):** mitigates but does not fully prevent DNS-based exfiltration via covert channels through the resolver
- **iptables rules require NET_ADMIN:** not available on all managed container services
- **Audit trail is best-effort:** network requests inferred from tool calls, not packet-level
- **Webhook SSRF mitigation is best-effort:** webhooks are validated against private/reserved IPs via double DNS resolution with a timing delay. Both resolutions must return public IPs, but the IPs need not be identical (to support CDN round-robin DNS used by services like Slack). A narrow TOCTOU window remains because httpx reconnects by hostname. Full elimination would require a custom transport that pins the validated IP at socket level, which httpx does not natively support. To further reduce risk, use a fixed set of trusted webhook domains and deploy behind an outbound proxy with destination policy enforcement
- **PR creation runs on the API host, not in a sandboxed container:** `create-pr` clones remote repos and runs git/gh commands directly on the API host. This is by design (the runner container doesn't have git push/gh access). The repo URL is validated at task creation time and only authenticated users can trigger PR creation. To limit blast radius, the API container runs as non-root and uses a Docker socket proxy
- **API key env var scrubbing is defense-in-depth, not a hard guarantee:** `ANTHROPIC_API_KEY` is removed from `os.environ` before the agent phase, but the original `/proc/1/environ` kernel memory from `execve` is not overwritable from userspace. The primary protection is privilege drop (`setuid` to uid 1000) — after which `/proc/1/environ` (owned by root) is not readable without `CAP_SYS_PTRACE`. For maximum security, consider using Docker secrets or tmpfs-mounted credential files instead of environment variables

See [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md) for the full list of architectural trade-offs.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Security Checklist for Contributors

When modifying security-critical code (`src/sandclaude/runner/container.py`, `src/sandclaude/auth.py`, `src/sandclaude/runner/entrypoint.py`, `docker-entrypoint-api.sh`):

- [ ] iptables rules still block all outbound except api.anthropic.com + allowed_domains
- [ ] Bearer tokens are never logged or exposed in error messages
- [ ] Webhook destinations are HTTPS and do not target localhost/private IP ranges
- [ ] API keys are never written to disk inside containers
- [ ] Container cleanup happens in `finally` blocks (no leaked containers)
- [ ] New env vars are documented in `.env.example`
