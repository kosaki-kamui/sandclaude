"""
SECURITY-CRITICAL: Docker container lifecycle + network isolation.

The network switch (setup-net - agent-net + iptables) is the primary
isolation boundary preventing code exfiltration by a compromised agent.
Changes to this file require careful security review.

Container lifecycle:
1. Create container on setup-net (full internet for deps)
2. Start container (setup phase: clone, install deps)
3. Wait for .setup-complete marker
4. Network switch: disconnect setup-net, connect agent-net
5. Apply iptables rules inside container (only api.anthropic.com:443)
6. Signal container to proceed (.network-switched marker)
7. Wait for completion, collect results
8. Cleanup container
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path

import docker

from sandclaude.config import settings
from sandclaude.db import store as db
from sandclaude.models import Task, TaskStatus

logger = logging.getLogger(__name__)

_client: docker.DockerClient | None = None

RUNNER_IMAGE = "sandclaude-runner:latest"
SETUP_NETWORK = "sandclaude-setup-net"
AGENT_NETWORK = "sandclaude-agent-net"
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")


def _sanitize_error_for_db(error: str) -> str:
    """Sanitize error text before DB persistence to avoid storing internal paths."""
    error = re.sub(r"(?:/[^\s:\"']+)+/?", "<path>", error)
    error = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", error)
    if len(error) > 2000:
        error = error[:2000] + "..."
    return error


def _is_valid_domain(domain: str) -> bool:
    return bool(DOMAIN_RE.match(domain))


def _resolve_and_validate_domain(domain: str) -> list[str]:
    """Resolve a domain to IPv4 addresses and validate all are public.

    Raises RuntimeError if any resolved IP is private/reserved/link-local.
    Returns the list of validated public IPv4 address strings.
    """
    import ipaddress
    import socket

    try:
        addrs = socket.getaddrinfo(domain, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        logger.warning(
            "DNS resolution failed for %s: %s — "
            "this domain will not be reachable in the agent phase",
            domain, exc,
        )
        raise RuntimeError(
            f"DNS resolution failed for allowed domain {domain}: {exc}. "
            f"The agent will not be able to reach this domain."
        )

    ipv4s: list[str] = []
    for family, _, _, _, sockaddr in addrs:
        ip_str = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        # Only IPv4 (iptables is IPv4-only; IPv6 is blocked entirely)
        if ip.version != 4:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            ip_class = (
                "private" if ip.is_private
                else "loopback" if ip.is_loopback
                else "link-local" if ip.is_link_local
                else "reserved"
            )
            logger.warning(
                "Allowed domain %s resolves to %s IP %s — "
                "blocked to prevent egress to internal services",
                domain, ip_class, ip_str,
            )
            raise RuntimeError(
                f"Allowed domain {domain} resolves to {ip_class} IP {ip_str}. "
                f"This is blocked to prevent egress to internal services. "
                f"Ensure {domain} resolves to a public IP."
            )
        if ip_str not in ipv4s:
            ipv4s.append(ip_str)

    if not ipv4s:
        logger.warning(
            "No IPv4 addresses resolved for %s — "
            "this domain may not be reachable if it only has IPv6 records",
            domain,
        )
    else:
        logger.info(
            "Resolved %s to %d IP(s): %s",
            domain, len(ipv4s), ", ".join(ipv4s),
        )
    return ipv4s


def _validate_local_path(path: str) -> None:
    """S1/S2: Validate that a local path is safe to mount as a volume.

    Raises RuntimeError if the path is not within allowed_repo_base (when configured),
    contains traversal patterns, or targets sensitive system directories.

    Uses os.path.realpath() to resolve symlinks — a symlink under an allowed base
    that points to /etc would be caught because realpath reveals the true target.
    """
    # Resolve symlinks and normalize (realpath resolves symlinks + normalizes)
    resolved = os.path.realpath(path)
    normalized = os.path.normpath(path)

    # If the path is a symlink, also check the direct link target (before
    # full resolution). On macOS /etc -> /private/etc, so realpath gives
    # /private/etc which wouldn't match /etc in our blocklist. By also
    # checking the direct symlink target, we catch this.
    paths_to_check = [normalized, resolved]
    if os.path.islink(path):
        link_target = os.path.normpath(os.readlink(path))
        if not os.path.isabs(link_target):
            link_target = os.path.normpath(os.path.join(os.path.dirname(path), link_target))
        paths_to_check.append(link_target)

    # Block traversal patterns
    for p in paths_to_check:
        if ".." in p.split(os.sep):
            raise RuntimeError(f"Path traversal detected in: {path}")

    # Block known sensitive paths (defense in depth for all environments).
    # Includes /private/ variants for macOS where /etc -> /private/etc, etc.
    _base_sensitive = (
        "/proc",
        "/sys",
        "/dev",
        "/var/run/docker",
        "/etc",
        "/root",
        "/boot",
        "/var/log",
        "/run",
        "/tmp",
    )
    sensitive_prefixes = _base_sensitive + tuple(f"/private{p}" for p in _base_sensitive)
    for check_path in paths_to_check:
        for prefix in sensitive_prefixes:
            if check_path == prefix or check_path.startswith(prefix + "/"):
                raise RuntimeError(f"Mounting sensitive system path is not allowed: {path}")

    # Block the root directory itself
    for check_path in paths_to_check:
        if check_path == "/":
            raise RuntimeError("Mounting the root filesystem is not allowed")

    # Enforce allowed_repo_base if configured
    if settings.allowed_repo_base:
        allowed_bases = [
            os.path.realpath(b.strip()) for b in settings.allowed_repo_base.split(",") if b.strip()
        ]
        under_allowed = any(
            resolved.startswith(base + "/") or resolved == base for base in allowed_bases
        )
        if not under_allowed:
            raise RuntimeError(
                f"Path {path} is not under any allowed base directory. "
                f"Configure ALLOWED_REPO_BASE to include it."
            )
    elif settings.environment.strip().lower() == "production":
        # In production without allowed_repo_base, only allow host_cwd from server config
        if settings.host_cwd:
            server_base = os.path.realpath(settings.host_cwd)
            if not (resolved.startswith(server_base + "/") or resolved == server_base):
                raise RuntimeError(
                    f"Path {path} is not under HOST_CWD ({settings.host_cwd}). "
                    f"Set ALLOWED_REPO_BASE to allow additional paths."
                )
        else:
            raise RuntimeError(
                "Local repo paths require HOST_CWD or ALLOWED_REPO_BASE "
                "in production. Set one of these environment variables."
            )


def _get_client() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def _host_path_for(container_path: Path) -> str:
    """Translate a container-local path to the equivalent host path.

    When the API runs inside Docker and spawns sibling containers via the
    Docker socket, volume mount source paths must be HOST paths (the Docker
    daemon resolves them on the host, not inside the API container).

    If HOST_DATA_DIR is set, paths under data_dir are translated.
    Otherwise the resolved container path is returned as-is (works when
    the API runs directly on the host).
    """
    resolved = str(container_path.resolve())
    if settings.host_data_dir:
        data_prefix = str(settings.data_dir.resolve())
        if resolved.startswith(data_prefix):
            return settings.host_data_dir + resolved[len(data_prefix) :]
    return resolved


async def _inject_task_secrets(task: Task, env: dict[str, str]) -> None:
    """Resolve declared secrets against policy and inject into container env.

    Secrets are declared in the task request, resolved against the policy preset,
    and injected as environment variables prefixed with SECRET_.
    Audit records are written for each secret (granted or denied).
    """
    import json as _json
    import os

    from sandclaude.db import store as _db
    from sandclaude.policy import check_secret_allowed, resolve_effective_policy

    if not task.declared_secrets:
        return

    try:
        secret_names = _json.loads(task.declared_secrets)
    except (ValueError, TypeError):
        return

    if not isinstance(secret_names, list):
        return

    policy = await resolve_effective_policy(task)

    for name in secret_names:
        if not isinstance(name, str) or not name:
            continue
        # Check policy allows this secret
        allowed = check_secret_allowed(policy, name)
        # Check server has the secret configured
        env_key = f"SECRET_{name}"
        value = os.environ.get(env_key, "")
        granted = allowed and bool(value)

        # Record in audit (name only, never value)
        await _db.record_task_secret(task.id, name, "setup", granted)

        if granted:
            env[name] = value
            logger.info("Secret %s granted for task %s", name, task.id)
        else:
            reason = "not in policy" if not allowed else "not configured"
            logger.info(
                "Secret %s denied for task %s (%s)", name, task.id, reason,
            )


async def run_task_in_container(task: Task) -> dict:
    """Full container lifecycle for a task. Returns result dict."""
    client = _get_client()
    data_dir = settings.data_dir
    output_dir = data_dir / "tasks" / task.id
    output_dir.mkdir(parents=True, exist_ok=True)

    # Resolve allowed domains early so the entrypoint/executor can audit against them
    all_allowed = _resolve_allowed_domains(task)

    env = {
        "TASK_ID": task.id,
        "TASK_PROMPT": task.prompt,
        "TASK_MODEL": task.model,
        "TASK_MAX_TURNS": str(task.max_turns),
        "TASK_TIMEOUT_S": str(settings.task_timeout_s),
        "ANTHROPIC_API_KEY": settings.anthropic_api_key,
        "ALLOWED_DOMAINS": ",".join(all_allowed),
    }

    # Pass git token for private repo cloning (setup phase only — scrubbed before agent phase)
    if settings.git_token:
        env["GIT_TOKEN"] = settings.git_token

    # v0.2.0: Inject declared secrets per policy
    await _inject_task_secrets(task, env)

    if task.repo == "." or task.repo.startswith("/"):
        env["LOCAL_REPO"] = "true"
    else:
        env["REPO_URL"] = task.repo

    if task.branch:
        env["REPO_BRANCH"] = task.branch

    # Build volume mounts — use host paths for DinD compatibility
    volumes: dict[str, dict[str, str]] = {
        _host_path_for(output_dir): {"bind": "/output", "mode": "rw"},
    }

    if task.repo == ".":
        # S2: Ignore client-provided host_cwd in production; use server config
        if settings.environment.strip().lower() == "production":
            source = settings.host_cwd or str(Path.cwd())
        else:
            source = task.host_cwd or settings.host_cwd or str(Path.cwd())
        _validate_local_path(source)
        volumes[source] = {"bind": "/workspace-source", "mode": "ro"}
    elif task.repo.startswith("/"):
        _validate_local_path(task.repo)
        volumes[task.repo] = {"bind": "/workspace-source", "mode": "ro"}

    container = None
    container_id = ""

    try:
        # Update status to setup
        await db.update_task(
            task.id,
            status=TaskStatus.setup,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        # Create container on setup-net
        container = await asyncio.to_thread(
            client.containers.run,
            image=RUNNER_IMAGE,
            environment=env,
            volumes=volumes,
            network=SETUP_NETWORK,
            detach=True,
            mem_limit="2g",
            cpu_shares=512,
            cap_add=["NET_ADMIN"],  # Required for in-container iptables
        )
        container_id = container.id
        await db.update_task(task.id, container_id=container_id)

        # Wait for setup to complete
        setup_marker = output_dir / ".setup-complete"
        setup_deadline = asyncio.get_running_loop().time() + 300  # 5 min

        while not setup_marker.exists():
            await asyncio.to_thread(container.reload)
            if container.status in ("exited", "dead"):
                exit_code = container.attrs.get("State", {}).get("ExitCode", -1)
                # Capture container logs before they're lost on removal
                try:
                    logs = await asyncio.to_thread(container.logs, tail=50, timestamps=False)
                    log_text = logs.decode("utf-8", errors="replace").strip()
                    logger.debug("Container logs:\n%s", log_text)
                except Exception:
                    log_text = ""
                raise RuntimeError(
                    f"Container exited during setup with code {exit_code}"
                    + (f"\n{log_text}" if log_text else "")
                )
            if asyncio.get_running_loop().time() > setup_deadline:
                raise RuntimeError("Setup phase timed out after 5 minutes")
            await asyncio.sleep(0.5)

        # Network switch
        await db.update_task(task.id, status=TaskStatus.running)

        if not settings.skip_network_isolation:
            # Reuse the domains resolved at container creation (avoid double DNS lookup)
            await _switch_to_agent_network(client, container, all_allowed)
        else:
            current_env = settings.environment.strip().lower()
            if current_env not in {"dev", "development", "test"}:
                raise RuntimeError(
                    "SKIP_NETWORK_ISOLATION=true is only allowed in development/test environments"
                )
            logger.info("SKIP_NETWORK_ISOLATION=true, skipping network switch")

        # Signal container to proceed to agent phase
        (output_dir / ".network-switched").write_text("")

        # Wait for container to finish
        result = await asyncio.to_thread(container.wait, timeout=settings.task_timeout_s)
        exit_code = result.get("StatusCode", -1)

        # Collect results (S15: validate values from container)
        result_path = output_dir / "result.json"
        if result_path.exists():
            result_data = json.loads(result_path.read_text())
            status = TaskStatus.completed if result_data.get("success") else TaskStatus.failed
            # Clamp/validate numeric fields from untrusted container output
            tokens_in = result_data.get("tokens_input")
            tokens_out = result_data.get("tokens_output")
            cost = result_data.get("total_cost_usd")
            if isinstance(tokens_in, (int, float)):
                tokens_in = max(0, min(int(tokens_in), 100_000_000))
            else:
                tokens_in = None
            if isinstance(tokens_out, (int, float)):
                tokens_out = max(0, min(int(tokens_out), 100_000_000))
            else:
                tokens_out = None
            if isinstance(cost, (int, float)):
                cost = max(0.0, min(float(cost), 100_000.0))
            else:
                cost = None
            error_str = result_data.get("error")
            if isinstance(error_str, str):
                error_str = _sanitize_error_for_db(error_str)
            else:
                error_str = None
            await db.update_task(
                task.id,
                status=status,
                completed_at=datetime.now(timezone.utc).isoformat(),
                tokens_input=tokens_in,
                tokens_output=tokens_out,
                total_cost_usd=cost,
                error=error_str,
            )
            return result_data
        else:
            error = f"Container exited with code {exit_code} without producing results"
            # Use conditional update to avoid overwriting cancelled status
            await db.update_task_if_status(
                task.id,
                expected_statuses=[TaskStatus.setup, TaskStatus.running],
                status=TaskStatus.failed,
                completed_at=datetime.now(timezone.utc).isoformat(),
                error=error,
            )
            return {"success": False, "error": error}

    except Exception as exc:
        error_msg = _sanitize_error_for_db(str(exc))
        # Use conditional update to avoid overwriting cancelled status
        await db.update_task_if_status(
            task.id,
            expected_statuses=[TaskStatus.queued, TaskStatus.setup, TaskStatus.running],
            status=TaskStatus.failed,
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=error_msg,
        )
        return {"success": False, "error": error_msg}

    finally:
        if container:
            try:
                await asyncio.to_thread(container.stop, timeout=5)
            except Exception:
                pass
            try:
                await asyncio.to_thread(container.remove, force=True)
            except Exception:
                pass


def _resolve_allowed_domains(task: Task) -> list[str]:
    """Merge global allowed_domains config with per-task overrides."""
    domains: list[str] = ["api.anthropic.com"]  # always allowed

    # Global config (comma-separated string)
    if settings.allowed_domains:
        for d in settings.allowed_domains.split(","):
            d = d.strip()
            if d and _is_valid_domain(d) and d not in domains:
                domains.append(d)

    # Per-task overrides (JSON-encoded list in DB)
    if task.allowed_domains:
        import json

        try:
            task_domains = json.loads(task.allowed_domains)
        except (json.JSONDecodeError, TypeError):
            task_domains = []
        for d in task_domains:
            d = d.strip()
            if d and _is_valid_domain(d) and d not in domains:
                domains.append(d)

    return domains


async def _switch_to_agent_network(
    client: docker.DockerClient,
    container: docker.models.containers.Container,
    allowed_domains: list[str],
) -> None:
    """
    Apply iptables rules, THEN disconnect from setup-net and connect to agent-net.

    S3 FIX: iptables rules are applied BEFORE the network switch to eliminate the
    race condition window where the container would be on agent-net without firewall
    rules. iptables rules are container-internal and persist across network changes.

    IMPORTANT: agent-net is NOT internal:true - that would block API calls.
    Instead, we use a regular bridge network + in-container iptables rules.
    Requires NET_ADMIN capability.

    allowed_domains: list of domain names to allow on port 443.
    Always includes api.anthropic.com. May also include package registries
    (registry.npmjs.org, pypi.org, etc.) so Claude can install deps.
    """
    # Pre-resolve domains on the host and validate all IPs are public.
    # This prevents allowed_domains from granting egress to internal services
    # (e.g., cloud metadata at 169.254.169.254, or internal hosts on 10.x.x.x)
    # via malicious/compromised DNS records.
    validated_ips: list[str] = []
    for domain in allowed_domains:
        if not _is_valid_domain(domain):
            raise RuntimeError(f"Invalid domain in allowlist: {domain}")
        domain_ips = _resolve_and_validate_domain(domain)
        validated_ips.extend(domain_ips)

    if not validated_ips:
        raise RuntimeError(
            "No valid public IPs resolved for allowed domains: " + ", ".join(allowed_domains)
        )

    # Build iptables rules using pre-validated IPs (no in-container DNS needed)
    _IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    ip_rules = ""
    for ip in validated_ips:
        if not _IPV4_RE.match(ip):
            raise RuntimeError(f"Invalid IPv4 address format in iptables rule: {ip}")
        ip_rules += f"iptables -A OUTPUT -d {shlex.quote(ip)} -p tcp --dport 443 -j ACCEPT && "

    # Apply iptables rules BEFORE network switch (S3: eliminates race condition).
    # DNS is restricted to Docker's internal resolver (127.0.0.11) only.
    # Also drop all IPv6 outbound to prevent bypass via IPv6.
    iptables_script = (
        "set -e && "
        "iptables -F OUTPUT && "
        "iptables -A OUTPUT -o lo -j ACCEPT && "
        "iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT && "
        "iptables -A OUTPUT -p udp --dport 53 -d 127.0.0.11 -j ACCEPT && "
        "iptables -A OUTPUT -p tcp --dport 53 -d 127.0.0.11 -j ACCEPT && "
        + ip_rules
        + "iptables -A OUTPUT -p icmp -j DROP && "
        "iptables -A OUTPUT -j DROP && "
        # IPv6 egress blocking — fail loudly if ip6tables is unavailable
        # rather than silently leaving IPv6 unrestricted.
        "ip6tables -F OUTPUT && "
        "ip6tables -A OUTPUT -o lo -j ACCEPT && "
        "ip6tables -A OUTPUT -j DROP"
    )
    exit_code, output = await asyncio.to_thread(
        container.exec_run, ["sh", "-c", iptables_script], user="root"
    )
    if exit_code != 0:
        raise RuntimeError(f"Failed to apply iptables rules (exit {exit_code}): {output.decode()}")

    # Now switch networks with iptables already in place
    setup_net = await asyncio.to_thread(client.networks.get, SETUP_NETWORK)
    agent_net = await asyncio.to_thread(client.networks.get, AGENT_NETWORK)

    await asyncio.to_thread(setup_net.disconnect, container)
    await asyncio.to_thread(agent_net.connect, container)


async def cancel_container(task: Task) -> bool:
    """Kill and mark a task as cancelled.

    Uses conditional DB updates to avoid clobbering a terminal status
    (completed/failed) that may have been set concurrently by the runner.
    """
    now = datetime.now(timezone.utc).isoformat()

    if task.status == TaskStatus.queued:
        return await db.update_task_if_status(
            task.id,
            expected_statuses=[TaskStatus.queued],
            status=TaskStatus.cancelled,
            completed_at=now,
        )

    if task.container_id:
        try:
            client = _get_client()
            container = await asyncio.to_thread(client.containers.get, task.container_id)
            await asyncio.to_thread(container.kill)
        except Exception:
            pass

    # Only transition to cancelled if task is still in a cancellable state
    updated = await db.update_task_if_status(
        task.id,
        expected_statuses=[TaskStatus.queued, TaskStatus.setup, TaskStatus.running],
        status=TaskStatus.cancelled,
        completed_at=now,
    )
    return updated


async def recover_orphans() -> None:
    """On startup, clean up tasks with dead/stale containers.

    For each orphaned task (status running/setup in DB):
    - If container is still running: stop and remove it
    - If container exists but stopped: remove it
    - If container is gone: just update DB
    Then mark the task as failed.
    """
    client = _get_client()
    orphans = await db.get_orphaned()

    for task in orphans:
        if task.container_id:
            try:
                container = await asyncio.to_thread(client.containers.get, task.container_id)
                # Stop running containers, then remove
                if container.status == "running":
                    try:
                        await asyncio.to_thread(container.stop, timeout=5)
                    except Exception:
                        pass
                try:
                    await asyncio.to_thread(container.remove, force=True)
                except Exception:
                    pass
            except Exception:
                pass  # Container already gone

        await db.update_task(
            task.id,
            status=TaskStatus.failed,
            completed_at=datetime.now(timezone.utc).isoformat(),
            error="server_restart_orphaned",
        )
        logger.info("Marked orphaned task %s as failed", task.id)
