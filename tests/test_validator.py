"""Tests for ValidatorHealthChecker and related components"""

import pytest
import responses

from monad_monitor.validator import (
    ValidatorHealthChecker,
    HealthStatus,
    SystemThresholds,
)
from monad_monitor.config import ValidatorConfig


# Sample Prometheus metrics response
HEALTHY_METRICS = """
monad_execution_ledger_num_commits 12345
monad_execution_ledger_block_num 98765
monad_state_consensus_events_local_timeout 0
monad_peer_disc_num_peers 25
monad_statesync_syncing 0
"""


class TestValidatorHealthChecker:
    """Test cases for ValidatorHealthChecker"""

    def test_check_returns_healthy_status(self, sample_validator_config):
        """Test that a healthy validator returns correct status"""
        with responses.RequestsMock() as rsps:
            # Mock metrics endpoint
            rsps.add(
                responses.GET,
                "http://192.168.1.100:8889/metrics",
                body=HEALTHY_METRICS,
                status=200,
            )
            # Mock RPC endpoint
            rsps.add(
                responses.POST,
                "http://192.168.1.100:8080",
                json={"jsonrpc": "2.0", "result": "0x123456", "id": 1},
                status=200,
            )

            checker = ValidatorHealthChecker(
                validator=sample_validator_config,
                timeout=10,
                thresholds=SystemThresholds(),
            )

            health_status, commits, exec_lagging, ts_validation_fail = checker.check()

            assert health_status.is_healthy is True
            assert health_status.block_height == 98765
            assert health_status.peers == 25
            assert commits == 12345

    def test_check_detects_stalled_block_production(self, sample_validator_config):
        """Test that stalled block production is detected"""
        with responses.RequestsMock() as rsps:
            # Mock metrics endpoint - need two calls for two checks
            rsps.add(
                responses.GET,
                "http://192.168.1.100:8889/metrics",
                body=HEALTHY_METRICS,
                status=200,
            )
            rsps.add(
                responses.GET,
                "http://192.168.1.100:8889/metrics",
                body=HEALTHY_METRICS,
                status=200,
            )
            # Mock RPC endpoint
            rsps.add(
                responses.POST,
                "http://192.168.1.100:8080",
                json={"jsonrpc": "2.0", "result": "0x123456", "id": 1},
                status=200,
            )
            rsps.add(
                responses.POST,
                "http://192.168.1.100:8080",
                json={"jsonrpc": "2.0", "result": "0x123456", "id": 1},
                status=200,
            )

            checker = ValidatorHealthChecker(
                validator=sample_validator_config,
                timeout=10,
            )

            # First check with no previous commits
            health_status_1, commits_1, _, _ = checker.check()
            assert health_status_1.is_healthy is True

            # Second check with same commits (simulating stalled production)
            health_status_2, commits_2, _, _ = checker.check(last_block_commits=commits_1)
            assert health_status_2.is_healthy is False
            assert "stopped producing" in health_status_2.message.lower()

    def test_check_handles_connection_failure(self, sample_validator_config):
        """Test that connection failures are handled gracefully"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://192.168.1.100:8889/metrics",
                body="Connection refused",
                status=500,
            )

            checker = ValidatorHealthChecker(
                validator=sample_validator_config,
                timeout=10,
            )

            health_status, commits, _, _ = checker.check()

            assert health_status.is_healthy is False
            assert "connection" in health_status.message.lower() or "failed" in health_status.message.lower()
            assert commits is None


class TestHealthStatus:
    """Test cases for HealthStatus dataclass"""

    def test_health_status_defaults(self):
        """Test that HealthStatus has correct defaults"""
        status = HealthStatus(
            is_healthy=True,
            message="OK",
        )

        assert status.metrics is None
        assert status.block_height is None
        assert status.peers is None
        assert status.is_syncing is False
        assert status.rpc_healthy is None
        assert status.warnings == []

    def test_health_status_with_warnings(self):
        """Test HealthStatus with warnings"""
        status = HealthStatus(
            is_healthy=True,
            message="OK with warnings",
            warnings=["CPU high", "Memory warning"],
        )

        assert status.is_healthy is True
        assert len(status.warnings) == 2


class TestSystemThresholds:
    """Test cases for SystemThresholds"""

    def test_default_thresholds(self):
        """Test default threshold values"""
        thresholds = SystemThresholds()

        assert thresholds.cpu_warning == 90.0
        assert thresholds.cpu_critical == 95.0
        assert thresholds.memory_warning == 90.0
        assert thresholds.memory_critical == 95.0
        assert thresholds.disk_warning == 85.0
        assert thresholds.disk_critical == 95.0

    def test_custom_thresholds(self):
        """Test custom threshold values"""
        thresholds = SystemThresholds(
            cpu_warning=80.0,
            cpu_critical=90.0,
            memory_warning=85.0,
            memory_critical=95.0,
            disk_warning=80.0,
            disk_critical=90.0,
        )

        assert thresholds.cpu_warning == 80.0
        assert thresholds.cpu_critical == 90.0


class TestValidatorConfig:
    """Test cases for ValidatorConfig"""

    def test_metrics_url_property(self, sample_validator_config):
        """Test metrics_url property generates correct URL"""
        expected = "http://192.168.1.100:8889/metrics"
        assert sample_validator_config.metrics_url == expected

    def test_rpc_url_property(self, sample_validator_config):
        """Test rpc_url property generates correct URL"""
        expected = "http://192.168.1.100:8080"
        assert sample_validator_config.rpc_url == expected

    def test_node_exporter_url_property(self, sample_validator_config):
        """Test node_exporter_url property generates correct URL"""
        expected = "http://192.168.1.100:9100/metrics"
        assert sample_validator_config.node_exporter_url == expected

    def test_node_exporter_url_none_when_not_configured(
        self, sample_validator_config_no_node_exporter
    ):
        """Test node_exporter_url is None when not configured"""
        assert sample_validator_config_no_node_exporter.node_exporter_url is None
