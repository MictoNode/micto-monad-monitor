"""Tests for the monitor dashboard HTTP server (port 8282)"""

import json
import time
import urllib.request

from monad_monitor.dashboard_server import DashboardServer

DASHBOARD_PORT = 18091
DASHBOARD_INDEX_PORT = 18092


class TestDashboardServerHealth:
    """Dashboard /health feeds the web UI and any external probe"""

    def test_health_reports_version_from_env_and_validators(self, monkeypatch):
        """Version comes from MONITOR_VERSION, not a hardcoded string"""
        monkeypatch.setenv("MONITOR_VERSION", "9.9.9")
        server = DashboardServer(host="127.0.0.1", port=DASHBOARD_PORT)
        server.start()
        try:
            server.update_validators(
                {"Validator1": {"state": "active", "healthy": True}},
                status="healthy",
                uptime_seconds=12.5,
            )

            data = None
            deadline = time.time() + 5
            while time.time() < deadline and data is None:
                try:
                    url = f"http://127.0.0.1:{DASHBOARD_PORT}/health"
                    with urllib.request.urlopen(url, timeout=2) as response:
                        data = json.loads(response.read().decode())
                except Exception:
                    time.sleep(0.1)

            assert data is not None, "dashboard /health did not respond"
            assert data["version"] == "9.9.9"
            assert data["status"] == "healthy"
            assert data["validators"]["Validator1"]["state"] == "active"
        finally:
            server.stop()

    def test_index_stamps_asset_urls_with_running_version(self, monkeypatch):
        """style.css / app.js are immutable-cached, so their URLs must carry
        the running version; otherwise a release never reaches the browser."""
        monkeypatch.setenv("MONITOR_VERSION", "9.9.9")
        server = DashboardServer(host="127.0.0.1", port=DASHBOARD_INDEX_PORT)
        server.start()
        try:
            html = None
            deadline = time.time() + 5
            while time.time() < deadline and html is None:
                try:
                    url = f"http://127.0.0.1:{DASHBOARD_INDEX_PORT}/"
                    with urllib.request.urlopen(url, timeout=2) as response:
                        html = response.read().decode()
                except Exception:
                    time.sleep(0.1)

            assert html is not None, "dashboard index did not respond"
            assert 'href="style.css?v=9.9.9"' in html
            assert 'src="app.js?v=9.9.9"' in html
        finally:
            server.stop()
