"""Tests for API server auth and endpoints."""
import math

import pytest

from monad_monitor.api_server import _safe_float


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

    def test_password_hashing(self):
        """bcrypt password verification works."""
        from monad_monitor.api_server import verify_password
        assert verify_password("testpass", "testpass") is True
        assert verify_password("wrong", "testpass") is False

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
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090", validators_config=[])
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
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090", validators_config=[])
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"password": "wrongpass"})
        assert response.status_code == 401

    def test_protected_endpoint_without_token(self):
        """Protected endpoint returns 401 without token."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090", validators_config=[])
        client = TestClient(app)
        response = client.get("/api/validators")
        assert response.status_code == 401

    def test_protected_endpoint_with_valid_token(self):
        """Protected endpoint returns 200 with valid token."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090", validators_config=[])
        client = TestClient(app)
        login = client.post("/api/auth/login", json={"password": "testpass"})
        token = login.json()["access_token"]
        response = client.get("/api/validators", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    def test_health_endpoint_no_auth(self):
        """Health endpoint works without authentication."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090", validators_config=[])
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

    def test_rpc_active_query_uses_sum_aggregate(self):
        """rpc_active uses sum() aggregate — metric tracks outgoing connections,
        not incoming RPC calls. 16 series all zero, aggregate gives single value."""
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        assert "rpc_active" in PROMETHEUS_QUERIES
        query = PROMETHEUS_QUERIES["rpc_active"]
        assert "sum(" in query, "rpc_active must use sum() aggregate"
        assert "monad_rpc_active_requests" in query
        formatted = query.format(name="TestVal")
        assert "{name}" not in formatted
        assert "TestVal" in formatted

    def test_rpc_exec_duration_queries_use_execution_metric(self):
        """rpc_exec_p50/p95/p99 use execution_duration histogram (per-method labels)."""
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        for key in ("rpc_exec_p50", "rpc_exec_p95", "rpc_exec_p99"):
            assert key in PROMETHEUS_QUERIES, f"{key} missing from PROMETHEUS_QUERIES"
            query = PROMETHEUS_QUERIES[key]
            assert "monad_rpc_execution_duration_seconds" in query, \
                f"{key} must use execution_duration metric"
            assert "histogram_quantile" in query, f"{key} must use histogram_quantile"
            assert 'type="total"' in query, f"{key} must filter type=total"
            formatted = query.format(name="TestVal")
            assert "{name}" not in formatted
            assert "TestVal" in formatted

    def test_old_rpc_duration_keys_removed(self):
        """Old rpc_duration_p50/p95/p99 keys must not exist (replaced by rpc_exec_*)."""
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        for old_key in ("rpc_duration_p50", "rpc_duration_p95", "rpc_duration_p99"):
            assert old_key not in PROMETHEUS_QUERIES, \
                f"Old key '{old_key}' should be replaced by rpc_exec_*"

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

    def test_overview_rpc_requests_uses_sum(self):
        """OVERVIEW_QUERIES rpc_requests must use sum() aggregate (same as rpc_active)."""
        from monad_monitor.api_server import OVERVIEW_QUERIES
        assert "rpc_requests" in OVERVIEW_QUERIES
        query = OVERVIEW_QUERIES["rpc_requests"]
        assert "sum(" in query, "rpc_requests must use sum() aggregate"
        formatted = query.format(name="TestVal")
        assert "{name}" not in formatted

    def test_rpc_call_rate_query_exists(self):
        """rpc_call_rate uses execution_duration_count with per-method breakdown."""
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        assert "rpc_call_rate" in PROMETHEUS_QUERIES
        query = PROMETHEUS_QUERIES["rpc_call_rate"]
        assert "execution_duration_seconds_count" in query
        assert "sum by(main)" in query
        assert 'type="total"' in query
        formatted = query.format(name="TestVal")
        assert "{name}" not in formatted
        assert "TestVal" in formatted

    def test_rpc_wait_time_queries_exist(self):
        """rpc_wait_p50/p95/p99 use execution_duration with type=wait."""
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        for key in ("rpc_wait_p50", "rpc_wait_p95", "rpc_wait_p99"):
            assert key in PROMETHEUS_QUERIES, f"{key} missing from PROMETHEUS_QUERIES"
            query = PROMETHEUS_QUERIES[key]
            assert "execution_duration_seconds" in query
            assert "histogram_quantile" in query
            assert 'type="wait"' in query, f"{key} must filter type=wait"
            formatted = query.format(name="TestVal")
            assert "{name}" not in formatted
            assert "TestVal" in formatted

    def test_rpc_methods_latency_query_exists(self):
        """rpc_methods_latency uses sum by(le,main) for per-method p99 breakdown."""
        from monad_monitor.api_server import PROMETHEUS_QUERIES
        assert "rpc_methods_latency" in PROMETHEUS_QUERIES
        query = PROMETHEUS_QUERIES["rpc_methods_latency"]
        assert "execution_duration_seconds" in query
        assert "histogram_quantile" in query
        assert "0.99" in query, "rpc_methods_latency must compute p99"
        assert "sum by(le,main)" in query, "must group by main for per-method breakdown"
        assert 'type="total"' in query
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
            "all": (2592000, "300s"),
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
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090", validators_config=[])
        client = TestClient(app)
        login = client.post("/api/auth/login", json={"password": "testpass"})
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/api/chart/TestVal/nonexistent?range=5m", headers=headers)
        assert response.status_code == 404  # metric not found, but range accepted

    def test_chart_endpoint_default_range_no_crash(self):
        """/api/chart/ works without range param (backward compatible)."""
        from fastapi.testclient import TestClient
        from monad_monitor.api_server import create_app
        app = create_app(password="testpass", jwt_secret="secret", prometheus_url="http://localhost:9090", validators_config=[])
        client = TestClient(app)
        login = client.post("/api/auth/login", json={"password": "testpass"})
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/api/chart/TestVal/nonexistent", headers=headers)
        assert response.status_code == 404  # same behavior, no crash
