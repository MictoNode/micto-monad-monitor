"""Tests for Health HTTP Server"""

import json
import socket
import time
import pytest
import threading
import urllib.request
import urllib.error

from monad_monitor.health_server import HealthServer, HealthStatus, freshness_state


def _free_port() -> int:
    """An ephemeral port, so a test never collides with a running monitor"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestHealthStatus:
    """Test cases for HealthStatus dataclass"""

    def test_default_health_status(self):
        """Test default health status values"""
        status = HealthStatus()
        assert status.status == "unknown"
        assert status.uptime_seconds == 0
        assert status.validators == {}

    def test_version_follows_monitor_version_env(self, monkeypatch):
        """Reported version is the one baked into the image"""
        monkeypatch.setenv("MONITOR_VERSION", "9.9.9")
        assert HealthStatus().version == "9.9.9"

    def test_version_defaults_for_source_builds(self, monkeypatch):
        """Without MONITOR_VERSION the documented default is reported"""
        monkeypatch.delenv("MONITOR_VERSION", raising=False)
        assert HealthStatus().version == "0.0.0"

    def test_health_status_to_dict(self):
        """Test converting health status to dict"""
        status = HealthStatus(
            status="healthy",
            validators={"Validator1": {"state": "active", "healthy": True}},
            version="2.0.0"
        )
        d = status.to_dict()

        assert d["status"] == "healthy"
        assert d["uptime_seconds"] >= 0  # Calculated from started_at
        assert d["validators"]["Validator1"]["state"] == "active"
        assert d["version"] == "2.0.0"
        assert "timestamp" in d

    def test_health_status_to_json(self):
        """Test converting health status to JSON"""
        status = HealthStatus(
            status="healthy",
            validators={},
            version="1.0.0"
        )
        json_str = status.to_json()
        data = json.loads(json_str)

        assert data["status"] == "healthy"
        assert data["uptime_seconds"] >= 0  # Calculated


class TestHealthServer:
    """Test cases for HealthServer"""

    def test_create_health_server(self):
        """Test creating a health server instance"""
        server = HealthServer(port=18080)
        assert server.port == 18080
        assert server.host == "0.0.0.0"

    def test_server_start_stop(self):
        """Test starting and stopping the health server"""
        server = HealthServer(port=18081)

        # Start server in background thread
        server.start()
        time.sleep(0.5)  # Wait for server to start

        try:
            # Set healthy status first
            server.update_status(is_healthy=True)

            # Make request to health endpoint
            url = "http://localhost:18081/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.status == 200
                data = json.loads(response.read().decode())
                assert "status" in data
        finally:
            server.stop()

    def test_health_endpoint_returns_json(self):
        """Test that /health endpoint returns valid JSON"""
        server = HealthServer(port=18082)
        server.start()
        time.sleep(0.5)

        try:
            # Set healthy status first
            server.update_status(is_healthy=True)

            url = "http://localhost:18082/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                content_type = response.headers.get("Content-Type", "")
                assert "application/json" in content_type

                data = json.loads(response.read().decode())
                assert isinstance(data, dict)
        finally:
            server.stop()

    def test_update_health_status(self):
        """Test updating health status"""
        server = HealthServer(port=18083)
        server.start()
        time.sleep(0.5)

        try:
            # Update status
            server.update_status(
                is_healthy=True,
                validators={"TestValidator": {"state": "active", "healthy": True, "height": 1000}}
            )

            # Check updated status
            url = "http://localhost:18083/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                assert data["status"] == "healthy"
                assert "TestValidator" in data["validators"]
                assert data["validators"]["TestValidator"]["height"] == 1000
        finally:
            server.stop()

    def test_unhealthy_validator_keeps_http_200(self):
        """Validator trouble must not mark the monitor container itself unhealthy

        The HTTP status answers "is the monitor loop ticking?"; the validator
        aggregate is reported in the body. A 503 here would fail the compose
        healthcheck (urlopen raises) and any external watchdog whenever a
        validator had a bad minute.
        """
        port = _free_port()
        server = HealthServer(port=port)
        server.start()
        time.sleep(0.5)

        try:
            server.update_status(
                is_healthy=False,
                validators={"TestValidator": {"state": "inactive", "healthy": False}},
                loop_tick=time.time(),
            )

            url = f"http://localhost:{port}/health"
            with urllib.request.urlopen(url, timeout=5) as response:
                assert response.status == 200
                data = json.loads(response.read().decode())
                assert data["status"] == "unhealthy"
                assert data["validators"]["TestValidator"]["healthy"] is False
                assert data["freshness"] == "ok"
        finally:
            server.stop()

    def test_stale_loop_returns_503(self):
        """A loop that stopped ticking is what turns /health into a 503"""
        port = _free_port()
        server = HealthServer(port=port, staleness_threshold=5.0)
        server.start()
        time.sleep(0.5)

        try:
            server.update_status(is_healthy=True, loop_tick=time.time() - 30)

            url = f"http://localhost:{port}/health"
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(url, timeout=5)
            assert exc_info.value.code == 503

            data = json.loads(exc_info.value.read().decode())
            assert data["freshness"] == "stale"
            assert data["check_age_seconds"] >= 29
        finally:
            server.stop()

    def test_readiness_endpoint(self):
        """Test /ready endpoint for Kubernetes readiness probes"""
        server = HealthServer(port=18085)
        server.start()
        time.sleep(0.5)

        try:
            url = "http://localhost:18085/ready"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.status == 200
                data = json.loads(response.read().decode())
                assert data["ready"] is True
        finally:
            server.stop()

    def test_liveness_endpoint(self):
        """Test /live endpoint for Kubernetes liveness probes"""
        server = HealthServer(port=18086)
        server.start()
        time.sleep(0.5)

        try:
            url = "http://localhost:18086/live"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.status == 200
                data = json.loads(response.read().decode())
                assert data["alive"] is True
        finally:
            server.stop()

    def test_metrics_endpoint(self):
        """Test /metrics endpoint for Prometheus scraping"""
        server = HealthServer(port=18087)
        server.start()
        time.sleep(0.5)

        try:
            url = "http://localhost:18087/metrics"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                content = response.read().decode()
                # Should contain Prometheus-formatted metrics
                assert "monad_monitor_" in content
        finally:
            server.stop()

    def test_404_for_unknown_path(self):
        """Test that unknown paths return 404"""
        server = HealthServer(port=18088)
        server.start()
        time.sleep(0.5)

        try:
            url = "http://localhost:18088/unknown"
            req = urllib.request.Request(url)
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req, timeout=5)
            assert exc_info.value.code == 404
        finally:
            server.stop()

    def test_uptime_increases(self):
        """Test that uptime increases over time"""
        server = HealthServer(port=18089)
        server.start()
        time.sleep(0.5)

        try:
            # Set healthy status first
            server.update_status(is_healthy=True)

            # Get initial uptime
            url = "http://localhost:18089/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                data1 = json.loads(response.read().decode())
                initial_uptime = data1["uptime_seconds"]

            # Wait a bit
            time.sleep(1)

            # Get updated uptime
            with urllib.request.urlopen(req, timeout=5) as response:
                data2 = json.loads(response.read().decode())
                updated_uptime = data2["uptime_seconds"]

            assert updated_uptime >= initial_uptime + 0.9
        finally:
            server.stop()

    def test_thread_safety(self):
        """Test that status updates are thread-safe"""
        server = HealthServer(port=18090)
        server.start()
        time.sleep(0.5)

        def update_status():
            for i in range(100):
                server.update_status(
                    is_healthy=True,
                    validators={"V": {"count": i}}
                )

        try:
            # Start multiple threads updating status
            threads = [threading.Thread(target=update_status) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Server should still respond correctly
            url = "http://localhost:18090/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.status == 200
        finally:
            server.stop()


class TestHealthFreshness:
    """Freshness of the monitor-loop heartbeat (what /health's status code means)"""

    def test_freshness_state_classifies_age(self):
        now = 1_000_000.0
        assert freshness_state(None, 300, now) == "unknown"
        assert freshness_state(now - 10, 300, now) == "ok"
        assert freshness_state(now - 301, 300, now) == "stale"

    def test_status_payload_reports_age_and_freshness(self):
        status = HealthStatus(
            last_check=time.time() - 5,
            staleness_threshold=300,
        )
        data = status.to_dict()
        assert data["freshness"] == "ok"
        assert 4.0 <= data["check_age_seconds"] <= 6.0
        assert status.is_fresh() is True

    def test_stale_status_is_not_fresh(self):
        status = HealthStatus(last_check=time.time() - 400, staleness_threshold=300)
        data = status.to_dict()
        assert data["freshness"] == "stale"
        assert status.is_fresh() is False

    def test_alert_stats_block_is_optional(self):
        assert "alerts" not in HealthStatus().to_dict()

        status = HealthStatus()
        server = HealthServer(port=_free_port())
        try:
            server.update_status(alerts={"telegram": {"sent": 2, "failed": 1, "consecutive_failures": 1}})
            data = server.get_health_status().to_dict()
        finally:
            server.stop()
        assert data["alerts"]["telegram"]["failed"] == 1

    def test_touch_heartbeat_resets_the_age(self):
        """The loop touches the heartbeat as it progresses (per second while waiting)"""
        server = HealthServer(port=_free_port(), staleness_threshold=5.0)
        try:
            server.update_status(loop_tick=time.time() - 30)
            assert server.get_health_status().is_fresh() is False

            server.touch_heartbeat()

            status = server.get_health_status()
            assert status.is_fresh() is True
            assert status.check_age_seconds() < 2
        finally:
            server.stop()


class TestUpdateStatusIsolation:
    """The loop keeps mutating its dicts; the served payload must not follow"""

    def test_caller_mutation_after_publish_is_isolated(self):
        server = HealthServer(port=_free_port())
        payload = {"V": {"state": "active", "height": 1}}
        server.update_status(is_healthy=True, validators=payload, loop_tick=time.time())

        # The monitor loop mutates its own dicts between iterations
        payload["V"]["height"] = 999
        payload["V"]["network_tps"] = 12.5
        payload["Other"] = {"state": "new"}

        served = server.get_health_status().to_dict()
        assert served["validators"]["V"]["height"] == 1
        assert "network_tps" not in served["validators"]["V"]
        assert "Other" not in served["validators"]

    def test_alert_stats_are_copied(self):
        server = HealthServer(port=_free_port())
        stats = {"telegram": {"sent": 1, "failed": 0, "consecutive_failures": 0}}
        server.update_status(alerts=stats)

        stats["telegram"]["sent"] = 99

        served = server.get_health_status().to_dict()
        assert served["alerts"]["telegram"]["sent"] == 1
