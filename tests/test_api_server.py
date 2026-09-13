"""Tests for API server auth and endpoints."""
import math

import pytest

from monad_monitor.api_server import LOGIN_FAILURE_LIMIT, _safe_float


class TestSafeFloat:
    """Test _safe_float sanitizes NaN/Inf for JSON compliance."""

    def test_normal_number(self):
        assert _safe_float(42.5) == 42.5

    def test_integer(self):
        assert _safe_float(10) == 10.0

    def test_string_number(self):
        assert _safe_float("3.14") == 3.14

    def test_nan_returns_none(self):
        assert _safe_float(float("nan")) is None

    def test_inf_returns_none(self):
        assert _safe_float(float("inf")) is None

    def test_negative_inf_returns_none(self):
        assert _safe_float(float("-inf")) is None

    def test_none_returns_none(self):
        assert _safe_float(None) is None

    def test_garbage_returns_none(self):
        assert _safe_float("abc") is None


class TestAuth:
    """Test JWT authentication."""

    def test_password_comparison_constant_time(self):
        """The configured password is compared in constant time (no bcrypt involved)."""
        from monad_monitor.api_server import verify_password
        assert verify_password("testpass", "testpass") is True
        assert verify_password("wrong", "testpass") is False
        assert verify_password("", "testpass") is False

    def test_jwt_create_and_verify(self):
        """JWT token creation and validation."""
        from monad_monitor.api_server import create_access_token, decode_token
        secret = "test-secret-key-12345"
        token = create_access_token(secret=secret)
        payload = decode_token(token, secret=secret)
        assert "exp" in payload
        assert "iat" in payload

    def test_jwt_expired(self):
        """Expired JWT raises exception."""
        from monad_monitor.api_server import create_access_token, decode_token
        import jwt
        secret = "test-secret-key-12345"
        token = create_access_token(secret=secret, expires_delta=-1)
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_token(token, secret=secret)

    def test_jwt_invalid_secret(self):
        """Wrong secret raises exception."""
        from monad_monitor.api_server import create_access_token, decode_token
        import jwt
        token = create_access_token(secret="correct-secret")
        with pytest.raises(jwt.InvalidSignatureError):
            decode_token(token, secret="wrong-secret")

    def test_login_success(self):
        """Login with correct password returns token."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090",
            validators_config=[{"name": "TestVal", "host": "1.2.3.4", "network": "testnet"}])
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"password": "testpass"})
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_wrong_password(self):
        """Login with wrong password returns 401."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090",
            validators_config=[{"name": "TestVal", "host": "1.2.3.4", "network": "testnet"}])
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"password": "wrongpass"})
        assert response.status_code == 401

    def test_protected_endpoint_without_token(self):
        """Protected endpoint returns 401 without token."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090",
            validators_config=[{"name": "TestVal", "host": "1.2.3.4", "network": "testnet"}])
        client = TestClient(app)
        response = client.get("/api/validators")
        assert response.status_code == 401

    def test_protected_endpoint_with_valid_token(self):
        """Protected endpoint returns 200 with valid token."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090",
            validators_config=[{"name": "TestVal", "host": "1.2.3.4", "network": "testnet"}])
        client = TestClient(app)
        login = client.post("/api/auth/login", json={"password": "testpass"})
        token = login.json()["access_token"]
        response = client.get("/api/validators", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    def test_health_endpoint_no_auth(self):
        """Health endpoint works without authentication."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090",
            validators_config=[{"name": "TestVal", "host": "1.2.3.4", "network": "testnet"}])
        client = TestClient(app)
        response = client.get("/api/health")
        assert response.status_code == 200


class TestQueries:
    """Test expanded Prometheus query templates."""

    def test_prometheus_queries_all_have_name_placeholder(self):
        """All PROMETHEUS_QUERIES keys format correctly with {name}."""
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        for key, query_tpl in PROMETHEUS_QUERIES.items():
            formatted = query_tpl.format(name="TestVal")
            assert "{name}" not in formatted, f"Key '{key}' still has unformatted {{name}}"
            assert "TestVal" in formatted, f"Key '{key}' missing TestVal in formatted output"

    def test_overview_queries_has_node_status(self):
        """OVERVIEW_QUERIES includes node_status key."""
        from monad_monitor.api_server import OVERVIEW_QUERIES
        assert "node_status" in OVERVIEW_QUERIES
        # Verify it formats correctly
        formatted = OVERVIEW_QUERIES["node_status"].format(name="TestVal")
        assert "TestVal" in formatted

    def test_prometheus_queries_count(self):
        """PROMETHEUS_QUERIES has expanded to cover all Grafana panels."""
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        assert len(PROMETHEUS_QUERIES) >= 71, f"Expected >=71 queries, got {len(PROMETHEUS_QUERIES)}"

    def test_tc_ratio_query_exists(self):
        """tc_ratio compound query exists with clamp_min protection."""
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        assert "tc_ratio" in PROMETHEUS_QUERIES, "tc_ratio key missing from PROMETHEUS_QUERIES"
        query = PROMETHEUS_QUERIES["tc_ratio"]
        # Must contain both process_tc and process_qc
        assert "process_tc" in query, "tc_ratio must reference process_tc"
        assert "process_qc" in query, "tc_ratio must reference process_qc"
        # Must have clamp_min for division-by-zero protection
        assert "clamp_min" in query, "tc_ratio must use clamp_min for safe division"
        # Must format correctly with {name}
        formatted = query.format(name="TestVal")
        assert "{name}" not in formatted
        assert "TestVal" in formatted

    def test_load_queries_normalized_by_cpu_count(self):
        """Load queries divide by CPU count (PromQL scalar)."""
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        for key in ("load1", "load5", "load15"):
            assert key in PROMETHEUS_QUERIES, f"{key} missing from PROMETHEUS_QUERIES"
            query = PROMETHEUS_QUERIES[key]
            assert "scalar" in query, f"{key} must use scalar() for CPU count"
            assert "node_cpu_seconds_total" in query, f"{key} must reference node_cpu_seconds_total"
            assert 'mode="idle"' in query, f"{key} must filter by mode=idle"
            # Must format correctly
            formatted = query.format(name="TestVal")
            assert "{name}" not in formatted
            assert "TestVal" in formatted

    def test_network_queries_filter_cni_devices(self):
        """Network queries exclude Kubernetes CNI devices (cali, cilium, flannel)."""
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        cni_patterns = ["cali.*", "cilium.*", "flannel.*"]
        for key in ("net_recv", "net_sent"):
            assert key in PROMETHEUS_QUERIES, f"{key} missing from PROMETHEUS_QUERIES"
            query = PROMETHEUS_QUERIES[key]
            for pattern in cni_patterns:
                assert pattern in query, f"{key} must filter out {pattern}"
            # Must also still have original filters
            for original in ["lo|", "veth.*", "docker.*", "br-.*"]:
                assert original in query, f"{key} must keep original filter {original}"

    # --- S44 Bug Fix Tests ---

    def test_memory_used_query_no_double_subtraction(self):
        """memory_used must be Total - Available only (not Total - Available - Cached - Buffers).

        MemAvailable already excludes Cached+Buffers. Subtracting them again
        produces deeply negative values (e.g. -71 GB).
        """
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        assert "memory_used" in PROMETHEUS_QUERIES
        query = PROMETHEUS_QUERIES["memory_used"]
        assert "MemTotal" in query, "memory_used must reference MemTotal"
        assert "MemAvailable" in query, "memory_used must reference MemAvailable"
        assert "Cached" not in query, "memory_used must NOT subtract Cached (already in MemAvailable)"
        assert "Buffers" not in query, "memory_used must NOT subtract Buffers (already in MemAvailable)"
        formatted = query.format(name="TestVal")
        assert "{name}" not in formatted
        assert "TestVal" in formatted

    def test_self_stake_bps_in_overview(self):
        """self_stake_bps must exist in OVERVIEW_QUERIES (backend passes raw bps)."""
        from monad_monitor.api_server import OVERVIEW_QUERIES
        assert "self_stake_bps" in OVERVIEW_QUERIES
        query = OVERVIEW_QUERIES["self_stake_bps"]
        assert "self_stake_bps" in query
        formatted = query.format(name="TestVal")
        assert "TestVal" in formatted

    def test_raptor_insertions_replaces_overquota(self):
        """raptor_overquota replaced by raptor_insertions (overquota metric absent from node)."""
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        assert "raptor_insertions" in PROMETHEUS_QUERIES
        assert "raptor_overquota" not in PROMETHEUS_QUERIES
        query = PROMETHEUS_QUERIES["raptor_insertions"]
        assert "p2p_total_insertions" in query
        formatted = query.format(name="TestVal")
        assert "{name}" not in formatted
        assert "TestVal" in formatted


class TestTimeRange:
    """Test time range parameter handling."""

    def test_valid_ranges_map_correctly(self):
        """Each valid range maps to correct start seconds and step."""
        from monad_monitor.api_server import _RANGE_CONFIG
        expected = {
            "1m": (60, "2s"),
            "5m": (300, "5s"),
            "30m": (1800, "15s"),
            "1h": (3600, "30s"),
            "24h": (86400, "60s"),
            "1w": (604800, "300s"),
            "1mo": (2592000, "600s"),
        }
        for key, (secs, step) in expected.items():
            assert key in _RANGE_CONFIG, f"Missing range: {key}"
            assert _RANGE_CONFIG[key] == (secs, step), f"Wrong config for {key}"

    def test_invalid_range_defaults_to_1h(self):
        """Invalid range value falls back to '1h'."""
        from monad_monitor.api_server import _resolve_range
        assert _resolve_range("garbage") == _resolve_range("1h")
        assert _resolve_range("") == _resolve_range("1h")
        assert _resolve_range("2h") == _resolve_range("1h")

    def test_chart_endpoint_accepts_range_param(self):
        """/api/chart/ accepts ?range= param without error."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090",
            validators_config=[{"name": "TestVal", "host": "1.2.3.4", "network": "testnet"}])
        client = TestClient(app)
        login = client.post("/api/auth/login", json={"password": "testpass"})
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/api/chart/TestVal/nonexistent?range=5m", headers=headers)
        assert response.status_code == 404  # metric not found, but range accepted
        assert "Unknown metric" in response.json()["detail"]

    def test_chart_endpoint_default_range_no_crash(self):
        """/api/chart/ works without range param (backward compatible)."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090",
            validators_config=[{"name": "TestVal", "host": "1.2.3.4", "network": "testnet"}])
        client = TestClient(app)
        login = client.post("/api/auth/login", json={"password": "testpass"})
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/api/chart/TestVal/nonexistent", headers=headers)
        assert response.status_code == 404  # same behavior, no crash
        assert "Unknown metric" in response.json()["detail"]


class TestRangeCache:
    """Test query_range caching: bucketed keys + per-range TTL + no-store header."""

    def test_range_ttl_mapping(self):
        """Long ranges get longer cache TTL; unknown defaults to 30."""
        from monad_monitor.api_server import _range_ttl
        assert _range_ttl("1m") == 10
        assert _range_ttl("1h") == 30
        assert _range_ttl("24h") == 60
        assert _range_ttl("1w") == 300
        assert _range_ttl("1mo") == 600
        assert _range_ttl("bogus") == 30

    def test_step_seconds_parsing(self):
        """Prometheus step strings like '600s' parse to integers."""
        from monad_monitor.api_server import _step_seconds
        for step, expected in [("2s", 2), ("5s", 5), ("30s", 30), ("300s", 300), ("600s", 600)]:
            assert _step_seconds(step) == expected

    def test_query_range_reuses_query_within_bucket(self, monkeypatch):
        """Consecutive polls in the same step bucket hit the cache (one Prometheus call)."""
        import asyncio
        from monad_monitor.api_server import PrometheusClient
        p = PrometheusClient("http://prom:9090")
        fake = _FakeClient()
        p._client = fake
        base = 1785990000.0

        async def run(offset):
            monkeypatch.setattr("monad_monitor.api_server.time.time", lambda: base + offset)
            return await p.query_range("up", range_param="1mo")

        r1 = asyncio.run(run(0))
        r2 = asyncio.run(run(100))   # same 600s bucket
        r3 = asyncio.run(run(500))   # same 600s bucket
        assert fake.calls == 1
        assert r1 == r2 == r3

    def test_query_range_new_bucket_requeries(self, monkeypatch):
        """Crossing a step bucket boundary issues a fresh Prometheus call."""
        import asyncio
        from monad_monitor.api_server import PrometheusClient
        p = PrometheusClient("http://prom:9090")
        fake = _FakeClient()
        p._client = fake
        base = 1785990000.0

        async def run(offset):
            monkeypatch.setattr("monad_monitor.api_server.time.time", lambda: base + offset)
            return await p.query_range("up", range_param="1mo")

        asyncio.run(run(0))
        asyncio.run(run(700))   # crosses the 600s bucket boundary
        assert fake.calls == 2

    def test_query_range_merges_version_duplicate_series(self, monkeypatch):
        """Series differing only in service_version merge into one continuous series."""
        import asyncio
        from monad_monitor.api_server import PrometheusClient
        p = PrometheusClient("http://prom:9090")
        fake = _FakeClientTwoSeries()
        p._client = fake
        base = 1785990000.0
        monkeypatch.setattr("monad_monitor.api_server.time.time", lambda: base)
        results = asyncio.run(p.query_range("up", range_param="1mo"))
        assert len(results) == 1
        values = results[0]["values"]
        assert len(values) == 6
        assert values[0][0] == base - 2592000
        assert values[-1][0] == base - 2592000 + 22 * 600
        ts = [v[0] for v in values]
        assert ts == sorted(ts)

    def test_api_responses_have_no_store_header(self):
        """All /api/* responses carry Cache-Control: no-store."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090",
            validators_config=[{"name": "TestVal", "host": "1.2.3.4", "network": "testnet"}])
        client = TestClient(app)
        login = client.post("/api/auth/login", json={"password": "testpass"})
        assert login.headers.get("cache-control") == "no-store"
        token = login.json()["access_token"]
        r = client.get("/api/validators", headers={"Authorization": f"Bearer {token}"})
        assert r.headers.get("cache-control") == "no-store"
        h = client.get("/api/health")
        assert h.headers.get("cache-control") == "no-store"

    def test_dashboard_html_revalidates(self):
        """The dashboard page carries the whole UI inline, so it must not be
        pinned by an edge cache after a release."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090",
            validators_config=[{"name": "TestVal", "host": "1.2.3.4", "network": "testnet"}])
        client = TestClient(app)
        r = client.get("/dashboard/")
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("text/html")
        assert r.headers.get("cache-control") == "no-cache"


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    """Minimal async httpx stand-in that counts outgoing calls."""
    def __init__(self):
        self.calls = 0
        self.is_closed = False

    async def get(self, url, params=None):
        self.calls += 1
        start = float(params.get("start", 0))
        step = int(params.get("step", "60s").rstrip("s"))
        values = [[start + i * step, f"{i + 1}.5"] for i in range(3)]
        return _FakeResponse({
            "status": "success",
            "data": {"resultType": "matrix", "result": [
                {"metric": {"name": "X"}, "values": values},
            ]},
        })


class _FakeClientTwoSeries:
    """Fake httpx client returning a stale and a fresh series (service_version split)."""
    def __init__(self):
        self.calls = 0
        self.is_closed = False

    async def get(self, url, params=None):
        self.calls += 1
        start = float(params.get("start", 0))
        step = int(params.get("step", "600s").rstrip("s"))
        stale = [[start + i * step, "1.5"] for i in range(3)]
        fresh = [[start + (i + 20) * step, "2.5"] for i in range(3)]
        return _FakeResponse({
            "status": "success",
            "data": {"resultType": "matrix", "result": [
                {"metric": {"service_version": "0.15.0"}, "values": stale},
                {"metric": {"service_version": "0.15.2"}, "values": fresh},
            ]},
        })


VALIDATORS = [{"name": "TestVal", "host": "1.2.3.4", "network": "testnet"}]


def _make_client(cookie_secure: str = "auto", validators_config=None):
    from fastapi.testclient import TestClient
    from monad_monitor.api_server import create_app
    app = create_app(
        password="testpass",
        jwt_secret="secret",
        prometheus_url="http://localhost:9090",
        validators_config=VALIDATORS if validators_config is None else validators_config,
        cookie_secure=cookie_secure,
    )
    return TestClient(app)


class TestLoginHardening:
    """Wrong passwords are budgeted; the correct one always gets in."""

    def test_wrong_passwords_are_throttled(self):
        client = _make_client()

        for _ in range(LOGIN_FAILURE_LIMIT):
            assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401

        throttled = client.post("/api/auth/login", json={"password": "nope"})

        assert throttled.status_code == 429
        assert int(throttled.headers["Retry-After"]) > 0

    def test_correct_password_still_works_when_budget_is_exhausted(self):
        """An attacker filling the budget must not lock the operator out."""
        client = _make_client()
        for _ in range(LOGIN_FAILURE_LIMIT + 5):
            client.post("/api/auth/login", json={"password": "nope"})

        assert client.post("/api/auth/login", json={"password": "testpass"}).status_code == 200

    def test_budget_is_scoped_to_the_app(self):
        throttled = _make_client()
        for _ in range(LOGIN_FAILURE_LIMIT + 1):
            throttled.post("/api/auth/login", json={"password": "nope"})
        assert throttled.post("/api/auth/login", json={"password": "nope"}).status_code == 429

        fresh = _make_client()
        assert fresh.post("/api/auth/login", json={"password": "nope"}).status_code == 401

    def test_malformed_bodies_are_rejected(self):
        client = _make_client()

        assert client.post(
            "/api/auth/login", content=b"{not json", headers={"Content-Type": "application/json"}
        ).status_code == 400
        assert client.post("/api/auth/login", json=["testpass"]).status_code == 400
        assert client.post("/api/auth/login", json={"password": 1234}).status_code == 400
        assert client.post("/api/auth/login", json={}).status_code == 400


class TestValidatorAllowList:
    """Only names from the config may be interpolated into PromQL."""

    def _session_headers(self, client):
        login = client.post("/api/auth/login", json={"password": "testpass"})
        return {"Authorization": f"Bearer {login.json()['access_token']}"}

    def test_configured_validator_is_accepted(self):
        """An empty series is still a successful query (Prometheus may be down)."""
        client = _make_client()

        response = client.get(
            "/api/chart/TestVal/block_height", headers=self._session_headers(client)
        )

        assert response.status_code == 200
        assert response.json()["validator"] == "TestVal"

    def test_unknown_validator_is_rejected(self):
        client = _make_client()
        headers = self._session_headers(client)
        injection = 'x"} or vector(1) or vector(1){x="'

        chart = client.get(f"/api/chart/{injection}/block_height", headers=headers)
        metrics = client.get(f"/api/metrics/{injection}", headers=headers)

        assert chart.status_code == 404
        assert metrics.status_code == 404
        assert "Unknown validator" in chart.json()["detail"]


class TestSecurityHeaders:
    """Defensive headers on every response."""

    def test_headers_on_api_and_dashboard(self):
        client = _make_client()

        for path in ("/api/health", "/dashboard/"):
            response = client.get(path)
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            csp = response.headers["Content-Security-Policy"]
            assert "frame-ancestors 'none'" in csp
            assert "object-src 'none'" in csp
            assert "https://cdn.jsdelivr.net" in csp


class TestCookieSecure:
    """DASHBOARD_COOKIE_SECURE: auto / always / never."""

    def _set_cookie(self, client, **kwargs) -> str:
        response = client.post("/api/auth/login", json={"password": "testpass"}, **kwargs)
        assert response.status_code == 200
        return response.headers["set-cookie"]

    def test_auto_marks_secure_when_a_proxy_reports_https(self):
        cookie = self._set_cookie(_make_client("auto"), headers={"X-Forwarded-Proto": "https"})
        assert "secure" in cookie.lower()

    def test_auto_keeps_plain_http_working(self):
        assert "secure" not in self._set_cookie(_make_client("auto")).lower()

    def test_always_forces_secure(self):
        assert "secure" in self._set_cookie(_make_client("always")).lower()

    def test_never_ignores_the_proxy_header(self):
        cookie = self._set_cookie(_make_client("never"), headers={"X-Forwarded-Proto": "https"})
        assert "secure" not in cookie.lower()

    def test_unknown_mode_falls_back_to_auto(self):
        cookie = self._set_cookie(_make_client("garbage"), headers={"X-Forwarded-Proto": "https"})
        assert "secure" in cookie.lower()
