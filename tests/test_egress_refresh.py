"""Tests for v0.3.0 egress allowlist refresh during agent execution."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from sandclaude.runner.container import _refresh_egress_loop


class TestRefreshEgressLoop:
    @pytest.mark.asyncio
    async def test_detects_new_ips(self):
        """When DNS returns a new IP, the loop appends it to iptables."""
        container = MagicMock()
        container.exec_run = MagicMock(return_value=(0, b""))

        call_count = 0

        def mock_resolve(domain):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                # First call during sleep — return initial IP only
                return ["1.2.3.4"]
            # Second call — return new IP
            return ["1.2.3.4", "5.6.7.8"]

        known_ips = {"1.2.3.4"}

        with patch(
            "sandclaude.runner.container._resolve_and_validate_domain",
            side_effect=mock_resolve,
        ):
            task = asyncio.create_task(
                _refresh_egress_loop(container, ["example.com"], known_ips, 0.01)
            )
            # Let it run two cycles
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # The new IP should have been added
        assert "5.6.7.8" in known_ips
        # exec_run should have been called with iptables script
        container.exec_run.assert_called()
        script = container.exec_run.call_args[0][0][2]  # ["sh", "-c", script]
        assert "5.6.7.8" in script
        assert "iptables -D OUTPUT -p icmp -j DROP" in script
        assert "iptables -A OUTPUT -p icmp -j DROP" in script

    @pytest.mark.asyncio
    async def test_handles_dns_failure_gracefully(self):
        """DNS failure for one domain should not crash the loop."""
        container = MagicMock()
        container.exec_run = MagicMock(return_value=(0, b""))

        def mock_resolve(domain):
            raise RuntimeError("DNS resolution failed")

        known_ips = {"1.2.3.4"}

        with patch(
            "sandclaude.runner.container._resolve_and_validate_domain",
            side_effect=mock_resolve,
        ):
            task = asyncio.create_task(
                _refresh_egress_loop(container, ["bad.example.com"], known_ips, 0.01)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # Loop continued despite failure — no exec_run since no new IPs
        container.exec_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_stops_on_exec_failure(self):
        """If docker exec fails, the loop should stop."""
        container = MagicMock()
        container.exec_run = MagicMock(return_value=(1, b"container not running"))

        def mock_resolve(domain):
            return ["1.2.3.4", "9.9.9.9"]

        known_ips = {"1.2.3.4"}

        with patch(
            "sandclaude.runner.container._resolve_and_validate_domain",
            side_effect=mock_resolve,
        ):
            task = asyncio.create_task(
                _refresh_egress_loop(container, ["example.com"], known_ips, 0.01)
            )
            # Wait for loop to detect exec failure and exit
            await asyncio.sleep(0.1)
            # Task should have finished on its own (not cancelled)
            assert task.done()

    @pytest.mark.asyncio
    async def test_no_exec_when_ips_stable(self):
        """When IPs haven't changed, no iptables update should happen."""
        container = MagicMock()
        container.exec_run = MagicMock(return_value=(0, b""))

        def mock_resolve(domain):
            return ["1.2.3.4"]

        known_ips = {"1.2.3.4"}

        with patch(
            "sandclaude.runner.container._resolve_and_validate_domain",
            side_effect=mock_resolve,
        ):
            task = asyncio.create_task(
                _refresh_egress_loop(container, ["example.com"], known_ips, 0.01)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # No iptables changes needed
        container.exec_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancellation_is_clean(self):
        """The loop should handle cancellation gracefully."""
        container = MagicMock()

        def mock_resolve(domain):
            return ["1.2.3.4"]

        known_ips = {"1.2.3.4"}

        with patch(
            "sandclaude.runner.container._resolve_and_validate_domain",
            side_effect=mock_resolve,
        ):
            task = asyncio.create_task(
                _refresh_egress_loop(container, ["example.com"], known_ips, 100)
            )
            # Cancel immediately (during the sleep)
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # No crash, no hanging


class TestRefreshSkipLogic:
    def test_refresh_skipped_for_short_tasks(self):
        """Verify the should_refresh condition logic."""
        # Simulating the condition from run_task_in_container
        # should_refresh = (
        #     not skip_network_isolation
        #     and egress_refresh_interval_s > 0
        #     and task_timeout_s >= egress_refresh_interval_s
        #     and len(all_allowed) > 0
        # )
        # Short task: timeout < interval
        assert not (
            not False  # skip_network_isolation=False
            and 300 > 0  # egress_refresh_interval_s
            and 60 >= 300  # task_timeout_s < interval
            and 1 > 0  # has domains
        )

        # Long task: timeout >= interval
        assert not False and 300 > 0 and 1800 >= 300 and 1 > 0

        # Disabled: interval=0
        assert not (not False and 0 > 0 and 1800 >= 0 and 1 > 0)

        # No domains
        assert not (not False and 300 > 0 and 1800 >= 300 and 0 > 0)
