"""Dashboard Server - Async HTTP server for serving the web dashboard

Serves the HTML dashboard and provides JSON API for validator data.
Runs on port 8282 (separate from health_server on 8181).
"""

import asyncio
import json
import threading
from pathlib import Path
from typing import Dict, Any, Optional

from aiohttp import web


class DashboardServer:
    """
    Async HTTP server for the web dashboard.

    Routes:
    - GET /          -> Serve index.html (dashboard UI)
    - GET /health    -> JSON API (validator data for dashboard)
    - GET /style.css -> Serve CSS stylesheet
    - GET /app.js    -> Serve JavaScript application

    Usage:
        server = DashboardServer(port=8282)
        server.start()
        # ... update validator data as needed ...
        server.update_validators(validator_data)
        # ... when done ...
        server.stop()
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8282):
        self.host = host
        self.port = port
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._validators_data: Dict[str, Dict[str, Any]] = {}
        self._monitor_status: str = "unknown"
        self._uptime_seconds: float = 0.0
        self._version: str = "1.0.0"
        self._network: str = "testnet"  # Default network
        self._lock = threading.Lock()

        # Path to static files
        self._static_dir = Path(__file__).parent / "static"

    async def _get_index(self, request: web.Request) -> web.Response:
        """Serve index.html"""
        index_path = self._static_dir / "index.html"
        if not index_path.exists():
            return web.Response(
                text="<html><body><h1>Dashboard not found</h1><p>Static files not installed.</p></body></html>",
                content_type="text/html",
                status=404
            )

        with open(index_path, "r", encoding="utf-8") as f:
            content = f.read()

        return web.Response(text=content, content_type="text/html")

    async def _get_health(self, request: web.Request) -> web.Response:
        """Return JSON with validator data for dashboard consumption"""
        with self._lock:
            data = {
                "status": self._monitor_status,
                "uptime_seconds": round(self._uptime_seconds, 2),
                "validators": dict(self._validators_data),
                "version": self._version,
                "network": self._network,
            }

        return web.json_response(data)

    async def _get_style_css(self, request: web.Request) -> web.Response:
        """Serve style.css"""
        css_path = self._static_dir / "style.css"
        if not css_path.exists():
            return web.Response(text="/* CSS not found */", content_type="text/css", status=404)

        with open(css_path, "r", encoding="utf-8") as f:
            content = f.read()

        return web.Response(text=content, content_type="text/css")

    async def _get_app_js(self, request: web.Request) -> web.Response:
        """Serve app.js"""
        js_path = self._static_dir / "app.js"
        if not js_path.exists():
            return web.Response(text="// JavaScript not found", content_type="application/javascript", status=404)

        with open(js_path, "r", encoding="utf-8") as f:
            content = f.read()

        return web.Response(text=content, content_type="application/javascript")

    def update_validators(
        self,
        validators: Dict[str, Dict[str, Any]],
        status: str = "healthy",
        uptime_seconds: float = 0.0,
        network: str = "testnet",
    ) -> None:
        """
        Update the validator data for the /health endpoint.

        Args:
            validators: Dict of validator name -> {state, healthy, height, peers, fails, huginn_data, ...}
            status: Overall monitor status ("healthy", "unhealthy", "unknown")
            uptime_seconds: Monitor uptime in seconds
            network: Network name ("testnet" or "mainnet")
        """
        with self._lock:
            self._validators_data = dict(validators)
            self._monitor_status = status
            self._uptime_seconds = uptime_seconds
            self._network = network

    def _create_app(self) -> web.Application:
        """Create and configure the aiohttp application"""
        app = web.Application()

        # Register routes
        app.router.add_get("/", self._get_index)
        app.router.add_get("/health", self._get_health)
        app.router.add_get("/style.css", self._get_style_css)
        app.router.add_get("/app.js", self._get_app_js)

        return app

    def _run_server(self) -> None:
        """Run the async server in a dedicated event loop"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self.app = self._create_app()
            self.runner = web.AppRunner(self.app)

            self._loop.run_until_complete(self.runner.setup())

            self.site = web.TCPSite(self.runner, self.host, self.port)
            self._loop.run_until_complete(self.site.start())

            # Keep the event loop running
            self._loop.run_forever()
        finally:
            # Cleanup on exit
            if self.runner:
                self._loop.run_until_complete(self.runner.cleanup())
            self._loop.close()

    def start(self) -> None:
        """Start the dashboard server in a background thread"""
        if self._thread is not None and self._thread.is_alive():
            return  # Already running

        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the dashboard server gracefully"""
        if self._loop and self._thread and self._thread.is_alive():
            # Schedule stop on the event loop
            self._loop.call_soon_threadsafe(self._loop.stop)

            # Wait for thread to finish
            self._thread.join(timeout=5.0)

        self._thread = None
        self._loop = None
        self.app = None
        self.runner = None
        self.site = None

    def is_running(self) -> bool:
        """Check if server is running"""
        return self._thread is not None and self._thread.is_alive()
