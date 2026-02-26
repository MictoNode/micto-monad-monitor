"""Tests for Health HTTP Server"""

import json
import time
import pytest
import threading
import urllib.request
import urllib.error

from monad_monitor.health_server import HealthServer, HealthStatus


class TestHealthStatus:
    """Test cases for HealthStatus dataclass"""

    def test_default_health_status(self):
        """Test default health status values"""
        status = HealthStatus()
        assert status.status == "unknown"
        assert status.uptime_seconds == 0
        assert status.validators == {}
        assert status.version == "1.0.0"

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

    def test_health_endpoint_shows_unhealthy(self):
        """Test that health endpoint reflects unhealthy state"""
        server = HealthServer(port=18084)
        server.start()
        time.sleep(0.5)

        try:
            # Update to unhealthy
            server.update_status(
                is_healthy=False,
                validators={"TestValidator": {"state": "inactive", "healthy": False}}
            )

            url = "http://localhost:18084/health"
            req = urllib.request.Request(url)
            # Expect 503 for unhealthy status
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req, timeout=5)
            assert exc_info.value.code == 503

            # Read the response body from the error
            response_data = exc_info.value.read().decode()
            data = json.loads(response_data)
            assert data["status"] == "unhealthy"
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
