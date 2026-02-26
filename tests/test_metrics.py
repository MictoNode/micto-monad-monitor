"""Tests for MetricsScraper and metric parsing"""

import pytest
import responses

from monad_monitor.metrics import MetricsScraper


# Sample Prometheus metrics response
SAMPLE_METRICS = """
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
"""

NODE_EXPORTER_METRICS = """
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


class TestMetricsScraper:
    """Test cases for MetricsScraper"""

    def test_fetch_metrics_success(self, sample_validator_config):
        """Test successful metrics fetch"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://192.168.1.100:8889/metrics",
                body=SAMPLE_METRICS,
                status=200,
            )

            scraper = MetricsScraper(
                metrics_url=sample_validator_config.metrics_url,
                rpc_url=sample_validator_config.rpc_url,
            )
            result = scraper.fetch_metrics()

            assert result is not None
            assert "monad_execution_ledger_num_commits" in result

    def test_fetch_metrics_failure(self, sample_validator_config):
        """Test metrics fetch handles failure"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://192.168.1.100:8889/metrics",
                body="Error",
                status=500,
            )

            scraper = MetricsScraper(
                metrics_url=sample_validator_config.metrics_url,
                rpc_url=sample_validator_config.rpc_url,
            )
            result = scraper.fetch_metrics()

            assert result is None

    def test_parse_metric_integer(self, metrics_scraper):
        """Test parsing integer metric value"""
        metrics_text = "monad_test_metric 42"
        result = metrics_scraper.parse_metric(metrics_text, "monad_test_metric")

        assert result == 42.0

    def test_parse_metric_float(self, metrics_scraper):
        """Test parsing float metric value"""
        metrics_text = "monad_test_metric 3.14159"
        result = metrics_scraper.parse_metric(metrics_text, "monad_test_metric")

        assert result == 3.14159

    def test_parse_metric_with_labels(self, metrics_scraper):
        """Test parsing metric with labels"""
        metrics_text = 'monad_test_metric{label="value"} 100'
        result = metrics_scraper.parse_metric(metrics_text, "monad_test_metric")

        assert result == 100.0

    def test_parse_metric_nan_value(self, metrics_scraper):
        """Test parsing NaN metric value returns None"""
        metrics_text = "monad_test_metric NaN"
        result = metrics_scraper.parse_metric(metrics_text, "monad_test_metric")

        assert result is None

    def test_parse_metric_not_found(self, metrics_scraper):
        """Test parsing non-existent metric returns None"""
        metrics_text = "some_other_metric 100"
        result = metrics_scraper.parse_metric(metrics_text, "monad_test_metric")

        assert result is None

    def test_parse_metric_scientific_notation_positive(self, metrics_scraper):
        """Test parsing scientific notation with positive exponent (e+07)"""
        metrics_text = "monad_execution_ledger_block_num 1.4896736e+07"
        result = metrics_scraper.parse_metric(metrics_text, "monad_execution_ledger_block_num")

        assert result == 14896736.0

    def test_parse_metric_scientific_notation_large(self, metrics_scraper):
        """Test parsing scientific notation for large values (e+09)"""
        metrics_text = "monad_test_metric 1.23456789e+09"
        result = metrics_scraper.parse_metric(metrics_text, "monad_test_metric")

        assert result == 1234567890.0

    def test_parse_metric_scientific_notation_negative_exponent(self, metrics_scraper):
        """Test parsing scientific notation with negative exponent"""
        metrics_text = "monad_test_metric 3.14e-5"
        result = metrics_scraper.parse_metric(metrics_text, "monad_test_metric")

        assert result == pytest.approx(0.0000314)

    def test_parse_metric_scientific_notation_with_labels(self, metrics_scraper):
        """Test parsing scientific notation with labels"""
        metrics_text = 'monad_test_metric{label="value"} 2.5e+06'
        result = metrics_scraper.parse_metric(metrics_text, "monad_test_metric")

        assert result == 2500000.0

    def test_parse_metric_integer_remains_compatible(self, metrics_scraper):
        """Test that regular integer notation still works (regression test)"""
        metrics_text = "monad_test_metric 12345"
        result = metrics_scraper.parse_metric(metrics_text, "monad_test_metric")

        assert result == 12345.0

    def test_get_monad_metrics_returns_dict(self, sample_validator_config):
        """Test get_monad_metrics returns dictionary"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://192.168.1.100:8889/metrics",
                body=SAMPLE_METRICS,
                status=200,
            )

            scraper = MetricsScraper(
                metrics_url=sample_validator_config.metrics_url,
                rpc_url=sample_validator_config.rpc_url,
            )
            result = scraper.get_monad_metrics()

            assert isinstance(result, dict)
            assert "block_commits" in result
            assert "block_height" in result

    def test_get_monad_metrics_parses_values(self, sample_validator_config):
        """Test get_monad_metrics correctly parses values"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://192.168.1.100:8889/metrics",
                body=SAMPLE_METRICS,
                status=200,
            )

            scraper = MetricsScraper(
                metrics_url=sample_validator_config.metrics_url,
                rpc_url=sample_validator_config.rpc_url,
            )
            result = scraper.get_monad_metrics()

            assert result["block_commits"] == 12345.0
            assert result["block_height"] == 98765.0
            assert result["local_timeout"] == 0.0
            assert result["peers"] == 25.0

    def test_get_monad_metrics_handles_error(self, sample_validator_config):
        """Test get_monad_metrics handles fetch errors"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://192.168.1.100:8889/metrics",
                body="",
                status=500,
            )

            scraper = MetricsScraper(
                metrics_url=sample_validator_config.metrics_url,
                rpc_url=sample_validator_config.rpc_url,
            )
            result = scraper.get_monad_metrics()

            assert "error" in result


class TestMetricsScraperRPCHealth:
    """Test RPC health check functionality"""

    def test_check_rpc_health_success(self, sample_validator_config):
        """Test successful RPC health check"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://192.168.1.100:8080",
                json={"jsonrpc": "2.0", "result": "0x123456", "id": 1},
                status=200,
            )

            scraper = MetricsScraper(
                metrics_url=sample_validator_config.metrics_url,
                rpc_url=sample_validator_config.rpc_url,
            )
            result = scraper.check_rpc_health()

            assert result is True

    def test_check_rpc_health_failure(self, sample_validator_config):
        """Test RPC health check handles failure"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://192.168.1.100:8080",
                json={"error": "Internal error"},
                status=500,
            )

            scraper = MetricsScraper(
                metrics_url=sample_validator_config.metrics_url,
                rpc_url=sample_validator_config.rpc_url,
            )
            result = scraper.check_rpc_health()

            assert result is False

    def test_check_rpc_health_timeout(self, sample_validator_config):
        """Test RPC health check handles timeout"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://192.168.1.100:8080",
                body=responses.ConnectionError("Timeout"),
            )

            scraper = MetricsScraper(
                metrics_url=sample_validator_config.metrics_url,
                rpc_url=sample_validator_config.rpc_url,
                timeout=1,
            )
            result = scraper.check_rpc_health()

            assert result is False


class TestMetricsScraperSystemMetrics:
    """Test system metrics parsing from Node Exporter"""

    def test_get_system_metrics_success(self, sample_validator_config):
        """Test successful system metrics fetch"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://192.168.1.100:9100/metrics",
                body=NODE_EXPORTER_METRICS,
                status=200,
            )

            scraper = MetricsScraper(
                metrics_url=sample_validator_config.metrics_url,
                rpc_url=sample_validator_config.rpc_url,
            )
            result = scraper.get_system_metrics(
                "http://192.168.1.100:9100/metrics"
            )

            assert "cpu_idle_percent" in result
            assert "mem_percent" in result
            assert "disk_percent" in result

    def test_get_system_metrics_no_url(self, metrics_scraper):
        """Test get_system_metrics returns empty dict when no URL"""
        result = metrics_scraper.get_system_metrics(None)
        assert result == {}

    def test_get_system_metrics_handles_failure(self, sample_validator_config):
        """Test system metrics handles fetch failure"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                "http://192.168.1.100:9100/metrics",
                body="Error",
                status=500,
            )

            scraper = MetricsScraper(
                metrics_url=sample_validator_config.metrics_url,
                rpc_url=sample_validator_config.rpc_url,
            )
            result = scraper.get_system_metrics(
                "http://192.168.1.100:9100/metrics"
            )

            assert result == {}

    def test_parse_cpu_idle(self, metrics_scraper):
        """Test CPU idle percentage calculation"""
        raw = """
node_cpu_seconds_total{cpu="0",mode="idle"} 800
node_cpu_seconds_total{cpu="0",mode="user"} 100
node_cpu_seconds_total{cpu="0",mode="system"} 100
"""
        result = metrics_scraper._parse_cpu_idle(raw)

        # Total: 1000, Idle: 800 -> 80%
        assert result == 80.0

    def test_parse_disk_metrics(self, metrics_scraper):
        """Test disk metrics parsing"""
        raw = """
node_filesystem_size_bytes{mount="/"} 100000
node_filesystem_avail_bytes{mount="/"} 25000
"""
        result = metrics_scraper._parse_disk_metrics(raw)

        assert result["total"] == 100000.0
        assert result["available"] == 25000.0
        assert result["used"] == 75000.0
        assert result["percent"] == 75.0


class TestGetValidatorStatusGmonadsFallback:
    """Test get_validator_status gmonads fallback when Huginn is unavailable"""

    def test_gmonads_fallback_when_huginn_returns_none(self, sample_validator_config):
        """Test that gmonads is used when Huginn returns None (unavailable)"""
        from unittest.mock import MagicMock

        scraper = MetricsScraper(
            metrics_url=sample_validator_config.metrics_url,
            rpc_url=sample_validator_config.rpc_url,
        )

        # Mock Huginn client that returns None (unavailable)
        mock_huginn = MagicMock()
        mock_huginn.get_validator_uptime.return_value = None

        # Mock gmonads client that returns True (validator is active)
        mock_gmonads = MagicMock()
        mock_gmonads.is_validator_in_active_set.return_value = True

        result = scraper.get_validator_status(
            validator_secp="02abc123",
            huginn_client=mock_huginn,
            network="testnet",
            gmonads_client=mock_gmonads,
        )

        assert result["is_active"] is True
        assert result["source"] == "gmonads_api"
        assert "gmonads" in result["reason"].lower()

    def test_gmonads_fallback_returns_false_for_inactive(self, sample_validator_config):
        """Test that gmonads fallback correctly identifies inactive validator"""
        from unittest.mock import MagicMock

        scraper = MetricsScraper(
            metrics_url=sample_validator_config.metrics_url,
            rpc_url=sample_validator_config.rpc_url,
        )

        # Mock Huginn client that returns None (unavailable)
        mock_huginn = MagicMock()
        mock_huginn.get_validator_uptime.return_value = None

        # Mock gmonads client that returns False (validator is NOT active)
        mock_gmonads = MagicMock()
        mock_gmonads.is_validator_in_active_set.return_value = False

        result = scraper.get_validator_status(
            validator_secp="02abc123",
            huginn_client=mock_huginn,
            network="testnet",
            gmonads_client=mock_gmonads,
        )

        assert result["is_active"] is False
        assert result["source"] == "gmonads_api"

    def test_gmonads_fallback_to_inference_when_both_fail(self, sample_validator_config):
        """Test that local inference is used when both Huginn and gmonads fail"""
        from unittest.mock import MagicMock

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                sample_validator_config.metrics_url,
                body=SAMPLE_METRICS,
                status=200,
            )

            scraper = MetricsScraper(
                metrics_url=sample_validator_config.metrics_url,
                rpc_url=sample_validator_config.rpc_url,
            )

            # Mock Huginn client that returns None (unavailable)
            mock_huginn = MagicMock()
            mock_huginn.get_validator_uptime.return_value = None

            # Mock gmonads client that returns None (cannot determine)
            mock_gmonads = MagicMock()
            mock_gmonads.is_validator_in_active_set.return_value = None

            result = scraper.get_validator_status(
                validator_secp="02abc123",
                huginn_client=mock_huginn,
                network="testnet",
                gmonads_client=mock_gmonads,
            )

            # Should fall back to local inference
            assert result["source"] == "inference"

    def test_gmonads_not_called_when_huginn_succeeds(self, sample_validator_config):
        """Test that gmonads is NOT called when Huginn succeeds"""
        from unittest.mock import MagicMock

        scraper = MetricsScraper(
            metrics_url=sample_validator_config.metrics_url,
            rpc_url=sample_validator_config.rpc_url,
        )

        # Mock Huginn client that returns valid data
        mock_huginn = MagicMock()
        mock_uptime = MagicMock()
        mock_uptime.is_active = True
        mock_uptime.total_events = 100
        mock_uptime.uptime_percent = 99.5
        mock_uptime.to_dict.return_value = {}
        mock_huginn.get_validator_uptime.return_value = mock_uptime

        # Mock gmonads client (should NOT be called)
        mock_gmonads = MagicMock()
        mock_gmonads.is_validator_in_active_set.return_value = True

        result = scraper.get_validator_status(
            validator_secp="02abc123",
            huginn_client=mock_huginn,
            network="testnet",
            gmonads_client=mock_gmonads,
        )

        # Result should come from Huginn
        assert result["source"] == "huginn_api"
        assert result["is_active"] is True

        # gmonads should NOT have been called
        mock_gmonads.is_validator_in_active_set.assert_not_called()

    def test_no_gmonads_client_uses_inference(self, sample_validator_config):
        """Test that local inference is used when gmonads_client is not provided"""
        from unittest.mock import MagicMock

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                sample_validator_config.metrics_url,
                body=SAMPLE_METRICS,
                status=200,
            )

            scraper = MetricsScraper(
                metrics_url=sample_validator_config.metrics_url,
                rpc_url=sample_validator_config.rpc_url,
            )

            # Mock Huginn client that returns None (unavailable)
            mock_huginn = MagicMock()
            mock_huginn.get_validator_uptime.return_value = None

            # No gmonads client provided
            result = scraper.get_validator_status(
                validator_secp="02abc123",
                huginn_client=mock_huginn,
                network="testnet",
                gmonads_client=None,  # No gmonads client
            )

            # Should fall back to local inference
            assert result["source"] == "inference"
