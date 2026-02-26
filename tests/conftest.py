"""Pytest configuration and shared fixtures for Monad Validator Monitor tests"""

import os
import sys
from typing import Dict, Any

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monad_monitor.config import ValidatorConfig
from monad_monitor.alerts import AlertHandler
from monad_monitor.metrics import MetricsScraper
from monad_monitor.validator import ValidatorHealthChecker, SystemThresholds


# Sample Prometheus metrics response for tests
SAMPLE_PROMETHEUS_METRICS = """
# HELP monad_execution_ledger_num_commits Number of block commits
# TYPE monad_execution_ledger_num_commits counter
monad_execution_ledger_num_commits 12345

# HELP monad_execution_ledger_block_num Current block height
# TYPE monad_execution_ledger_block_num gauge
monad_execution_ledger_block_num 98765

# HELP monad_state_consensus_events_local_timeout Local timeout events
# TYPE monad_state_consensus_events_local_timeout counter
monad_state_consensus_events_local_timeout 0

# HELP monad_peer_disc_num_peers Number of connected peers
# TYPE monad_peer_disc_num_peers gauge
monad_peer_disc_num_peers 25

# HELP monad_statesync_syncing Sync status
# TYPE monad_statesync_syncing gauge
monad_statesync_syncing 0

# HELP mc_current_epoch Current epoch
# TYPE mc_current_epoch gauge
mc_current_epoch 42

# HELP mc_current_round Current round
# TYPE mc_current_round gauge
mc_current_round 7
"""

SAMPLE_NODE_EXPORTER_METRICS = """
# HELP node_cpu_seconds_total Seconds the CPUs spent in each mode
# TYPE node_cpu_seconds_total counter
node_cpu_seconds_total{cpu="0",mode="idle"} 1000
node_cpu_seconds_total{cpu="0",mode="user"} 100
node_cpu_seconds_total{cpu="0",mode="system"} 50
node_cpu_seconds_total{cpu="1",mode="idle"} 1000
node_cpu_seconds_total{cpu="1",mode="user"} 100
node_cpu_seconds_total{cpu="1",mode="system"} 50

# HELP node_memory_MemTotal_bytes Total memory
# TYPE node_memory_MemTotal_bytes gauge
node_memory_MemTotal_bytes 16777216000

# HELP node_memory_MemAvailable_bytes Available memory
# TYPE node_memory_MemAvailable_bytes gauge
node_memory_MemAvailable_bytes 8388608000

# HELP node_filesystem_size_bytes Filesystem size
# TYPE node_filesystem_size_bytes gauge
node_filesystem_size_bytes{mount="/"} 107374182400

# HELP node_filesystem_avail_bytes Filesystem available
# TYPE node_filesystem_avail_bytes gauge
node_filesystem_avail_bytes{mount="/"} 53687091200
"""


@pytest.fixture
def sample_validator_config() -> ValidatorConfig:
    """Create a sample validator configuration for testing"""
    return ValidatorConfig(
        name="test-validator",
        host="192.168.1.100",
        metrics_port=8889,
        rpc_port=8080,
        node_exporter_port=9100,
        validator_secp="0x1234567890abcdef",
        enabled=True,
    )


@pytest.fixture
def sample_validator_config_no_node_exporter() -> ValidatorConfig:
    """Create a validator config without node exporter"""
    return ValidatorConfig(
        name="test-validator-basic",
        host="192.168.1.101",
        metrics_port=8889,
        rpc_port=8080,
        node_exporter_port=None,
        validator_secp="0xabcdef1234567890",
        enabled=True,
    )


@pytest.fixture
def sample_config() -> Dict[str, Any]:
    """Create a sample configuration dictionary"""
    return {
        "telegram": {
            "token": "test-telegram-token",
            "chat_id": "test-chat-id",
        },
        "pushover": {
            "user_key": "test-user-key",
            "app_token": "test-app-token",
        },
        "monitoring": {
            "check_interval": 60,
            "alert_threshold": 3,
            "health_report_interval": 3600,
            "timeout": 10,
        },
        "thresholds": {
            "cpu_warning": 90,
            "cpu_critical": 95,
            "memory_warning": 90,
            "memory_critical": 95,
            "disk_warning": 85,
            "disk_critical": 95,
        },
    }


@pytest.fixture
def alert_handler(sample_config) -> AlertHandler:
    """Create an AlertHandler instance for testing"""
    return AlertHandler(
        telegram_token=sample_config["telegram"]["token"],
        telegram_chat_id=sample_config["telegram"]["chat_id"],
        pushover_user_key=sample_config["pushover"]["user_key"],
        pushover_app_token=sample_config["pushover"]["app_token"],
    )


@pytest.fixture
def metrics_scraper(sample_validator_config) -> MetricsScraper:
    """Create a MetricsScraper instance for testing"""
    return MetricsScraper(
        metrics_url=sample_validator_config.metrics_url,
        rpc_url=sample_validator_config.rpc_url,
        timeout=10,
    )


@pytest.fixture
def health_checker(sample_validator_config) -> ValidatorHealthChecker:
    """Create a ValidatorHealthChecker instance for testing"""
    return ValidatorHealthChecker(
        validator=sample_validator_config,
        timeout=10,
        thresholds=SystemThresholds(),
    )


@pytest.fixture
def system_thresholds() -> SystemThresholds:
    """Create default system thresholds"""
    return SystemThresholds()
