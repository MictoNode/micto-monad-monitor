"""Tests for active validator detection functionality"""

import pytest
import responses

from monad_monitor.validator import (
    ValidatorHealthChecker,
    HealthStatus,
    SystemThresholds,
)
from monad_monitor.config import ValidatorConfig
from monad_monitor.metrics import MetricsScraper


# Sample metrics responses
HEALTHY_METRICS = """
monad_execution_ledger_num_commits 12345
monad_execution_ledger_block_num 98765
monad_state_consensus_events_local_timeout 0
monad_bft_txpool_create_proposal 10
monad_peer_disc_num_peers 25
monad_statesync_syncing 0
"""

# Metrics with active validator
ACTIVE_VALIDATOR_METRICS = """
monad_consensus_active_validators 10
monad_bft_txpool_create_proposal 15
monad_consensus_proposed_blocks_total 100
monad_consensus_signed_blocks_total 200
monad_consensus_missed_blocks_total 5
"""

# Metrics with inactive validator (has local timeout)
INACTIVE_VALIDATOR_METRICS = """
monad_consensus_active_validators 10
monad_bft_txpool_create_proposal 0
monad_state_consensus_events_local_timeout 5
"""


class TestActiveValidatorDetection:
    """Test cases for active validator detection"""

    @pytest.fixture
    def validator_config(self):
        """Create validator config for testing"""
        return ValidatorConfig(
            name="test-validator",
            host="192.168.1.100",
            metrics_port=8889,
            rpc_port=8080,
            node_exporter_port=None,
            validator_secp="0x1234567890abcdef",
            enabled=True,
        )

    @pytest.fixture
    def health_checker(self, validator_config):
        """Create health checker for testing"""
        return ValidatorHealthChecker(
            validator=validator_config,
            timeout=10,
            thresholds=SystemThresholds(),
        )

    def test_get_validator_status_active(self, validator_config):
        """Test getting validator active status when active"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://192.168.1.100:8889/metrics",
                body=ACTIVE_VALIDATOR_METRICS,
                status=200,
            )

            scraper = MetricsScraper(
                metrics_url=validator_config.metrics_url,
                rpc_url=validator_config.rpc_url,
            )
            status = scraper.get_validator_status(validator_config.validator_secp)

            assert status["is_active"] is True
            assert "reason" in status
            assert status["metrics_used"] != []

    def test_get_validator_status_inactive(self, validator_config):
        """Test getting validator status when cannot determine from local metrics alone

        Note: local_timeout is no longer used to determine validator status because
        it tracks OTHER nodes' timeouts, not our validator's status. When we can't
        determine status from proposals/commits, we return None (unknown).
        """
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://192.168.1.100:8889/metrics",
                body=INACTIVE_VALIDATOR_METRICS,
                status=200,
            )

            scraper = MetricsScraper(
                metrics_url=validator_config.metrics_url,
                rpc_url=validator_config.rpc_url,
            )
            status = scraper.get_validator_status(validator_config.validator_secp)

            # Without proposals/commits and without local_timeout logic,
            # we can't determine status from local metrics alone
            assert status["is_active"] is None  # Unknown - cannot determine
            assert "Cannot determine" in status["reason"] or "no proposals" in status["reason"].lower()

    def test_get_validator_status_fetch_error(self, validator_config):
        """Test getting validator status when metrics fetch fails"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://192.168.1.100:8889/metrics",
                body="Error",
                status=500,
            )

            scraper = MetricsScraper(
                metrics_url=validator_config.metrics_url,
                rpc_url=validator_config.rpc_url,
            )
            status = scraper.get_validator_status(validator_config.validator_secp)

            assert status["is_active"] is None  # Unknown
            assert "Could not fetch" in status["reason"]
