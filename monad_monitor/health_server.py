"""Lightweight HTTP Health Server for monitoring the monitor"""

import json
import time
import threading
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, Optional


VERSION = "1.0.0"


@dataclass
class HealthStatus:
    """Health status data for the monitor"""
    status: str = "unknown"  # "healthy", "unhealthy", "unknown"
    uptime_seconds: float = 0.0
    validators: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    version: str = VERSION
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "status": self.status,
            "uptime_seconds": round(time.time() - self.started_at, 2),
            "validators": self.validators,
            "version": self.version,
            "timestamp": time.time(),
        }

    def to_json(self) -> str:
        """Convert to JSON string"""
        return json.dumps(self.to_dict(), indent=2)


class HealthRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for health endpoints"""

    # Class-level reference to health server (set by HealthServer)
    health_server: Optional["HealthServer"] = None

    def log_message(self, format, *args):
        """Suppress default logging (or redirect to custom logger)"""
        pass  # Silent by default

    def _send_json_response(self, data: Dict[str, Any], status_code: int = 200):
        """Send a JSON response"""
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text_response(self, text: str, status_code: int = 200):
        """Send a plain text response"""
        body = text.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Handle GET requests"""
        if self.health_server is None:
            self._send_json_response({"error": "Server not initialized"}, 500)
            return

        path = self.path.split("?")[0]  # Strip query params

        if path == "/health":
            self._handle_health()
        elif path == "/ready":
            self._handle_ready()
        elif path == "/live":
            self._handle_live()
        elif path == "/metrics":
            self._handle_metrics()
        else:
            self._send_json_response({"error": "Not found"}, 404)

    def _handle_health(self):
        """Handle /health endpoint - full health status"""
        status = self.health_server.get_health_status()
        http_status = 200 if status.status == "healthy" else 503
        self._send_json_response(status.to_dict(), http_status)

    def _handle_ready(self):
        """Handle /ready endpoint - Kubernetes readiness probe"""
        # Ready if server is running (could add dependency checks here)
        self._send_json_response({"ready": True, "timestamp": time.time()}, 200)

    def _handle_live(self):
        """Handle /live endpoint - Kubernetes liveness probe"""
        # Alive if server responds
        self._send_json_response({"alive": True, "timestamp": time.time()}, 200)

    def _handle_metrics(self):
        """Handle /metrics endpoint - Prometheus format"""
        status = self.health_server.get_health_status()
        lines = []

        # Monitor status
        lines.append(f"monad_monitor_status{{version=\"{status.version}\"}} {1 if status.status == 'healthy' else 0}")

        # Uptime
        lines.append(f"monad_monitor_uptime_seconds {status.uptime_seconds:.2f}")

        # Validator count
        lines.append(f"monad_monitor_validators_total {len(status.validators)}")

        # Per-validator metrics
        for name, v in status.validators.items():
            safe_name = name.replace("-", "_").replace(" ", "_")
            state_value = 1 if v.get("state") == "active" else 0
            healthy_value = 1 if v.get("healthy", False) else 0

            lines.append(f"monad_monitor_validator_active{{name=\"{safe_name}\"}} {state_value}")
            lines.append(f"monad_monitor_validator_healthy{{name=\"{safe_name}\"}} {healthy_value}")

            if "height" in v:
                lines.append(f"monad_monitor_validator_height{{name=\"{safe_name}\"}} {v['height']}")

        self._send_text_response("\n".join(lines) + "\n", 200)


class HealthServer:
    """
    Lightweight HTTP server for health checks.

    Endpoints:
    - GET /health - Full health status (JSON)
    - GET /ready - Readiness probe (for Kubernetes)
    - GET /live - Liveness probe (for Kubernetes)
    - GET /metrics - Prometheus metrics

    Usage:
        server = HealthServer(port=8080)
        server.start()
        # ... update status as needed ...
        server.update_status(is_healthy=True, validators={...})
        # ... when done ...
        server.stop()
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self._status = HealthStatus()
        self._lock = threading.Lock()
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the health server in a background thread"""
        if self._server is not None:
            return  # Already running

        # Set up handler with reference to this server
        HealthRequestHandler.health_server = self

        self._server = HTTPServer((self.host, self.port), HealthRequestHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the health server"""
        if self._server is not None:
            self._server.shutdown()
            self._server = None
            self._thread = None

    def update_status(
        self,
        is_healthy: Optional[bool] = None,
        validators: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        """
        Update the health status.

        Args:
            is_healthy: Overall health status (None to keep current)
            validators: Dict of validator name -> {state, healthy, height, ...}
        """
        with self._lock:
            if is_healthy is not None:
                self._status.status = "healthy" if is_healthy else "unhealthy"

            if validators is not None:
                self._status.validators = validators

    def get_health_status(self) -> HealthStatus:
        """Get current health status (thread-safe copy)"""
        with self._lock:
            # Return a copy with updated uptime
            status = HealthStatus(
                status=self._status.status,
                uptime_seconds=time.time() - self._status.started_at,
                validators=dict(self._status.validators),
                version=self._status.version,
                started_at=self._status.started_at,
            )
            return status

    def is_running(self) -> bool:
        """Check if server is running"""
        return self._server is not None
