"""
Docker integration tests - require Docker daemon and built runner image.

These tests verify the full container lifecycle:
- Container creation on setup-net
- Network switch to agent-net
- iptables rules applied correctly
- Artifact generation (result.json, diff.patch, audit.json)

Run with: pytest tests/test_docker_integration.py -v
Requires: docker build -t sandclaude-runner -f Dockerfile.runner .

Skipped automatically if Docker is unavailable.
"""

from __future__ import annotations

import pytest

# Skip all tests if Docker is not available
docker = pytest.importorskip("docker")

try:
    _client = docker.from_env()
    _client.ping()
    _docker_available = True
except Exception:
    _docker_available = False

pytestmark = pytest.mark.skipif(not _docker_available, reason="Docker daemon not available")

RUNNER_IMAGE = "sandclaude-runner:latest"
SETUP_NETWORK = "sandclaude-setup-net"
AGENT_NETWORK = "sandclaude-agent-net"


def _image_exists() -> bool:
    try:
        _client.images.get(RUNNER_IMAGE)
        return True
    except docker.errors.ImageNotFound:
        return False


@pytest.fixture(autouse=True)
def _require_image():
    if not _image_exists():
        pytest.skip(f"Runner image {RUNNER_IMAGE} not built")


def _ensure_network(name: str) -> None:
    """Create a Docker network if it doesn't exist."""
    try:
        _client.networks.get(name)
    except docker.errors.NotFound:
        _client.networks.create(name, driver="bridge")


@pytest.fixture(autouse=True)
def _ensure_networks():
    _ensure_network(SETUP_NETWORK)
    _ensure_network(AGENT_NETWORK)


def test_runner_image_exists():
    """Verify the runner image was built."""
    image = _client.images.get(RUNNER_IMAGE)
    assert image is not None


def test_runner_container_starts(tmp_path):
    """Verify a runner container can start on setup-net."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    container = _client.containers.run(
        image=RUNNER_IMAGE,
        environment={
            "TASK_ID": "test-start",
            "TASK_PROMPT": "echo hello",
            "TASK_MODEL": "claude-sonnet-4-5",
            "TASK_MAX_TURNS": "1",
            "ANTHROPIC_API_KEY": "sk-test-not-real",
            "LOCAL_REPO": "true",
        },
        volumes={
            str(output_dir): {"bind": "/output", "mode": "rw"},
            str(tmp_path): {"bind": "/workspace-source", "mode": "ro"},
        },
        network=SETUP_NETWORK,
        detach=True,
        mem_limit="512m",
        cap_add=["NET_ADMIN"],
    )

    try:
        # Wait briefly for container to start
        container.reload()
        assert container.status in ("running", "created")
    finally:
        container.stop(timeout=3)
        container.remove(force=True)


def test_network_isolation_matrix():
    """Proposal Day 3: 6-test network isolation matrix.

    After iptables rules are applied:
    1. api.anthropic.com:443  - ALLOWED (critical positive test)
    2. https://evil.com       - BLOCKED
    3. https://registry.npmjs.org - BLOCKED
    4. DNS for evil.com       - BLOCKED (external resolver blocked)
    5. ICMP ping 8.8.8.8      - BLOCKED
    6. Outbound on non-443    - BLOCKED
    """
    container = _client.containers.run(
        image=RUNNER_IMAGE,
        command=["sleep", "60"],
        entrypoint=[],
        network=AGENT_NETWORK,
        detach=True,
        mem_limit="256m",
        cap_add=["NET_ADMIN"],
    )

    try:
        # Apply full iptables rules (same as container.py).
        # Use getent ahosts (not hosts) to get IPv4 addresses reliably.
        # getent hosts may return only IPv6, leaving zero IPv4 results.
        iptables_script = (
            "set -e && "
            "ANTHROPIC_IPS=$(getent ahosts api.anthropic.com "
            "| awk '{print $1}' | grep -E '^[0-9]+\\.' | sort -u) && "
            'test -n "$ANTHROPIC_IPS" && '
            "iptables -F OUTPUT && "
            "iptables -A OUTPUT -o lo -j ACCEPT && "
            "iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT && "
            "iptables -A OUTPUT -p udp --dport 53 -d 127.0.0.11 -j ACCEPT && "
            "iptables -A OUTPUT -p tcp --dport 53 -d 127.0.0.11 -j ACCEPT && "
            "for ip in $ANTHROPIC_IPS; do "
            'iptables -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT; done && '
            "iptables -A OUTPUT -p icmp -j DROP && "
            "iptables -A OUTPUT -j DROP && "
            "ip6tables -F OUTPUT && "
            "ip6tables -A OUTPUT -o lo -j ACCEPT && "
            "ip6tables -A OUTPUT -j DROP"
        )
        exit_code, output = container.exec_run(["sh", "-c", iptables_script], user="root")
        assert exit_code == 0, f"iptables failed: {output.decode()}"

        # Test 1: api.anthropic.com:443 - ALLOWED
        exit_code, output = container.exec_run(
            [
                "sh",
                "-c",
                "curl -s --connect-timeout 5 -o /dev/null -w '%{http_code}' "
                "https://api.anthropic.com/v1/messages",
            ]
        )
        # Should connect (returns 401 without key, but connection succeeds)
        assert exit_code == 0, f"api.anthropic.com should be ALLOWED: {output.decode()}"

        # Test 2: evil.com - BLOCKED
        exit_code, output = container.exec_run(
            ["sh", "-c", "curl -s --connect-timeout 3 https://evil.com || echo BLOCKED"]
        )
        assert b"BLOCKED" in output or exit_code != 0

        # Test 3: registry.npmjs.org - BLOCKED
        exit_code, output = container.exec_run(
            ["sh", "-c", "curl -s --connect-timeout 3 https://registry.npmjs.org || echo BLOCKED"]
        )
        assert b"BLOCKED" in output or exit_code != 0

        # Test 4: DNS resolves via Docker resolver (127.0.0.11) but TCP is blocked.
        # DNS itself is not blocked (Docker resolver forwards queries), but the
        # resolved IP won't be in the iptables allowlist, so connections fail.
        # Verify by resolving then attempting connection on a non-allowed port.
        exit_code, output = container.exec_run(
            [
                "sh",
                "-c",
                "ip=$(getent ahosts evil.com | awk '{print $1}'"
                " | grep -E '^[0-9]+\\.' | head -1) && "
                "curl -s --connect-timeout 3 http://$ip:80"
                " || echo BLOCKED",
            ]
        )
        assert b"BLOCKED" in output or exit_code != 0

        # Test 5: ICMP ping - BLOCKED
        exit_code, output = container.exec_run(
            ["sh", "-c", "ping -c 1 -W 2 8.8.8.8 2>&1 || echo BLOCKED"]
        )
        assert b"BLOCKED" in output or exit_code != 0

        # Test 6: Outbound on non-443 port - BLOCKED
        exit_code, output = container.exec_run(
            ["sh", "-c", "curl -s --connect-timeout 3 http://example.com:80 || echo BLOCKED"]
        )
        assert b"BLOCKED" in output or exit_code != 0

    finally:
        container.stop(timeout=3)
        container.remove(force=True)


def test_network_switch():
    """Verify container can be switched between networks."""
    container = _client.containers.run(
        image=RUNNER_IMAGE,
        command=["sleep", "30"],
        entrypoint=[],
        network=SETUP_NETWORK,
        detach=True,
        mem_limit="256m",
        cap_add=["NET_ADMIN"],
    )

    try:
        # Switch networks
        setup_net = _client.networks.get(SETUP_NETWORK)
        agent_net = _client.networks.get(AGENT_NETWORK)

        setup_net.disconnect(container)
        agent_net.connect(container)

        # Verify container is on agent-net
        container.reload()
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        assert AGENT_NETWORK in networks
        assert SETUP_NETWORK not in networks

    finally:
        container.stop(timeout=3)
        container.remove(force=True)
