"""Tests for HealthReporter and health report generation"""

import time

import pytest
import responses

from monad_monitor.alerts import AlertHandler
from monad_monitor.config import ValidatorConfig
from monad_monitor.health_report import HealthReporter


class TestHealthReporter:
    """Test cases for HealthReporter"""

    @pytest.fixture
    def alert_handler(self):
        """Create AlertHandler for testing"""
        return AlertHandler(
            telegram_token="test-telegram-token",
            telegram_chat_id="test-chat-id",
            pushover_user_key="test-user-key",
            pushover_app_token="test-app-token",
        )

    @pytest.fixture
    def reporter(self, alert_handler):
        """Create HealthReporter with short interval for testing"""
        return HealthReporter(
            alerts=alert_handler,
            report_interval=1,  # 1 second for testing
        )

    @pytest.fixture
    def sample_validators(self):
        """Create sample validator list"""
        return [
            ValidatorConfig(
                name="validator-1",
                host="192.168.1.100",
                metrics_port=8889,
                rpc_port=8080,
                node_exporter_port=None,
                validator_secp="0x1234",
                enabled=True,
            ),
            ValidatorConfig(
                name="validator-2",
                host="192.168.1.101",
                metrics_port=8889,
                rpc_port=8080,
                node_exporter_port=None,
                validator_secp="0x5678",
                enabled=True,
            ),
        ]

    @pytest.fixture
    def sample_states(self):
        """Create sample validator states"""
        return {
            "validator-1": {
                "fails": 0,
                "alert_active": False,
                "last_height": 100000,
                "last_peers": 25,
            },
            "validator-2": {
                "fails": 3,
                "alert_active": True,
                "last_height": 99000,
                "last_peers": 10,
            },
        }

    def test_maybe_send_report_returns_false_before_interval(
        self, reporter, sample_validators, sample_states
    ):
        """Test report not sent before interval elapses"""
        # Set last report time to now so interval hasn't elapsed
        reporter.last_report_time = time.time()

        result = reporter.maybe_send_report(sample_validators, sample_states)

        assert result is False

    def test_maybe_send_report_returns_true_after_interval(
        self, reporter, sample_validators, sample_states
    ):
        """Test report sent after interval elapses"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            reporter.last_report_time = time.time() - 2  # 2 seconds ago

            result = reporter.maybe_send_report(sample_validators, sample_states)

            assert result is True

    def test_send_report_includes_all_validators(
        self, reporter, sample_validators, sample_states
    ):
        """Test report includes all validators"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )

            reporter.maybe_send_report(sample_validators, sample_states)

            request_body = rsps.calls[0].request.body
            assert "validator-1" in str(request_body)
            assert "validator-2" in str(request_body)

    def test_send_report_shows_health_status(
        self, reporter, sample_validators, sample_states
    ):
        """Test report shows healthy/unhealthy status"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )

            reporter.maybe_send_report(sample_validators, sample_states)

            request_body = rsps.calls[0].request.body
            body_str = str(request_body)
            assert "Summary" in body_str or "Healthy" in body_str or "Unhealthy" in body_str

    def test_send_startup_report(self, reporter, sample_validators):
        """Test startup report is sent correctly"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )

            reporter.send_startup_report(sample_validators)

            request_body = rsps.calls[0].request.body
            assert "Started" in str(request_body) or "started" in str(request_body).lower()
            assert "validator-1" in str(request_body)
            assert "validator-2" in str(request_body)

    def test_send_shutdown_report(self, reporter):
        """Test shutdown report is sent correctly"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )

            reporter.send_shutdown_report()

            request_body = rsps.calls[0].request.body
            assert "Stopped" in str(request_body) or "stopped" in str(request_body).lower()

    def test_report_updates_last_report_time(
        self, reporter, sample_validators, sample_states
    ):
        """Test that last_report_time is updated after sending"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            original_time = reporter.last_report_time
            reporter.last_report_time = time.time() - 2

            reporter.maybe_send_report(sample_validators, sample_states)

            assert reporter.last_report_time > original_time


class TestHealthReporterExtendedReport:
    """Test cases for extended health reports (6-hour feature)"""

    @pytest.fixture
    def alert_handler(self):
        """Create AlertHandler for testing"""
        return AlertHandler(
            telegram_token="test-token",
            telegram_chat_id="test-chat",
        )

    @pytest.fixture
    def reporter(self, alert_handler):
        """Create HealthReporter with extended report capability"""
        return HealthReporter(
            alerts=alert_handler,
            report_interval=3600,
            extended_report_interval=1,  # 1 second for testing
        )

    @pytest.fixture
    def sample_validators(self):
        """Create sample validator list"""
        return [
            ValidatorConfig(
                name="validator-1",
                host="192.168.1.100",
                metrics_port=8889,
                rpc_port=8080,
                node_exporter_port=None,
                validator_secp="0x1234",
                enabled=True,
            ),
        ]

    @pytest.fixture
    def sample_states(self):
        """Create sample validator states"""
        return {
            "validator-1": {
                "fails": 0,
                "alert_active": False,
                "last_height": 100000,
                "last_peers": 25,
            },
        }

    def test_extended_report_interface_exists(self, reporter):
        """Verify extended report interface can be added"""
        assert hasattr(reporter, "alerts")
        assert hasattr(reporter, "report_interval")
        assert hasattr(reporter, "extended_report_interval")
        assert hasattr(reporter, "maybe_send_extended_report")

    def test_extended_report_config_attribute(self, alert_handler):
        """Test extended report configuration can be set"""
        reporter = HealthReporter(
            alerts=alert_handler,
            report_interval=3600,
            extended_report_interval=21600,  # 6 hours
        )

        assert reporter.report_interval == 3600
        assert reporter.extended_report_interval == 21600

    def test_extended_report_returns_false_before_interval(
        self, reporter, sample_validators, sample_states
    ):
        """Test extended report not sent before interval elapses"""
        reporter.last_extended_report_time = time.time()

        result = reporter.maybe_send_extended_report(sample_validators, sample_states)

        assert result is False

    def test_extended_report_returns_true_after_interval(
        self, reporter, sample_validators, sample_states
    ):
        """Test extended report sent after interval elapses"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            reporter.last_extended_report_time = time.time() - 2  # 2 seconds ago

            result = reporter.maybe_send_extended_report(sample_validators, sample_states)

            assert result is True

    def test_extended_report_includes_block_metrics(
        self, reporter, sample_validators, sample_states
    ):
        """Test extended report includes block production metrics"""
        metrics_data = {
            "validator-1": {
                "is_active_validator": True,
                "proposed_blocks": 10,
                "signed_blocks": 50,
                "missed_blocks": 2,
                "local_timeout": 0,
            }
        }

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            reporter.last_extended_report_time = time.time() - 2

            reporter.maybe_send_extended_report(
                sample_validators, sample_states, metrics_data
            )

            request_body = rsps.calls[0].request.body
            body_str = str(request_body)
            # Check for extended report indicators
            assert "Extended" in body_str or "Proposed" in body_str or "Signed" in body_str

    def test_extended_report_shows_inactive_validator(
        self, reporter, sample_validators, sample_states
    ):
        """Test extended report shows inactive validator status"""
        metrics_data = {
            "validator-1": {
                "is_active_validator": False,
                "proposed_blocks": 0,
                "signed_blocks": 0,
                "missed_blocks": 0,
                "local_timeout": 5,
            }
        }

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            reporter.last_extended_report_time = time.time() - 2

            reporter.maybe_send_extended_report(
                sample_validators, sample_states, metrics_data
            )

            request_body = rsps.calls[0].request.body
            body_str = str(request_body)
            # Should show Inactive status
            assert "Inactive" in body_str or "inactive" in body_str.lower()
