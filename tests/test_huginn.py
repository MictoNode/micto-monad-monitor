"""Tests for Huginn API client with multi-network support"""

import time
import pytest
import responses

from monad_monitor.huginn import HuginnConfig, HuginnClient, ValidatorUptime, CircuitBreaker, CircuitState


# Sample API responses (Huginn Validator API v2 shape).
# v2 uptime responses no longer carry last_round / last_block_height /
# since_utc - those come from the /health endpoint (see SAMPLE_HEALTH_RESPONSE).
SAMPLE_ACTIVE_VALIDATOR_RESPONSE = {
    "validator_id": 42,
    "validator_name": "Test Validator",
    "secp_address": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
    "status": "active",
    "finalized_count": 1500,
    "timeout_count": 0,
    "total_events": 1500,
    "uptime_percent": 100.0,
}

SAMPLE_INACTIVE_VALIDATOR_RESPONSE = {
    "validator_id": 42,
    "validator_name": "Test Validator",
    "secp_address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdef",
    "status": "inactive",
    "finalized_count": 0,
    "timeout_count": 0,
    "total_events": 0,
    "uptime_percent": 0.0,
}

SAMPLE_PENDING_VALIDATOR_RESPONSE = {
    "validator_id": 43,
    "validator_name": "Pending Validator",
    "secp_address": "0x111111111111111111111111111111111111111111111111111111111111111111",
    "status": "pending",
    "finalized_count": 1999,
    "timeout_count": 1,
    "total_events": 2000,
    "uptime_percent": 99.95,
}

# /validators/{idOrAddress}/health - restores last_round/last_block_height
# and adds the purpose-built liveness view.
SAMPLE_HEALTH_RESPONSE = {
    "success": True,
    "health": {
        "validator_id": 42,
        "validator_name": "Test Validator",
        "status": "active",
        "state": "healthy",
        "healthy": True,
        "threshold": 98,
        "uptime_24h": 99.9,
        "last_round": 51712837,
        "last_block_height": 12345678,
        "last_event_ts": 1704067200,
        "last_event_utc": "2026-09-09T15:41:28.000Z",
        "seconds_since_last_event": 6,
        "snapshot_coverage": {"epochs": 709, "first_epoch": 439, "last_epoch": 1221},
    },
}

# /status - data freshness of Huginn's own indexer
SAMPLE_STATUS_FRESH_RESPONSE = {
    "success": True,
    "network": "testnet",
    "uptime_events": {"count": 571204, "seconds_since_newest": 5},
}

SAMPLE_STATUS_STALE_RESPONSE = {
    "success": True,
    "network": "testnet",
    "uptime_events": {"count": 571204, "seconds_since_newest": 600},
}

# /validators/uptime/{secp}?period=30d - the middle horizon between the rolling
# 24h window and the all-time cumulative totals.
SAMPLE_UPTIME_30D_RESPONSE = {
    "success": True,
    "period": "30d",
    "uptime": {
        "validator_id": 42,
        "validator_name": "Test Validator",
        "status": "active",
        "finalized_count": 32300,
        "timeout_count": 119,
        "total_events": 32419,
    },
}

# /staking/validator-set - consensus vs next-epoch vs eligible sets
SAMPLE_VALIDATOR_SET_RESPONSE = {
    "success": True,
    "epoch": 1233,
    "in_delay_period": False,
    "counts": {
        "active": 200,
        "next_epoch": 200,
        "eligible": 208,
        "entering": 8,
        "leaving": 2,
        "eligible_not_active": 8,
    },
    "entering": [{"validator_id": 231, "name": "Example", "stake": 10500000}],
    "leaving": [
        {"validator_id": 67, "name": "Unity Nodes", "stake": 11000000},
        {"validator_id": 68, "name": "Imperator.co", "stake": 11000000},
    ],
    "eligible_not_active": [],
}

# Endpoint URLs
TESTNET_API = "https://validator-api-testnet.huginn.tech/monad-api"
MAINNET_API = "https://validator-api.huginn.tech/monad-api"


class TestCircuitBreaker:
    """Test cases for Circuit Breaker"""

    def test_initial_state_is_closed(self):
        """Circuit breaker should start in CLOSED state"""
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute() is True

    def test_opens_after_threshold_failures(self):
        """Circuit breaker should open after threshold failures"""
        cb = CircuitBreaker(failure_threshold=3)

        for _ in range(3):
            cb.record_failure()

        assert cb.state == CircuitState.OPEN
        assert cb.can_execute() is False
        assert cb.is_open() is True

    def test_success_resets_failures(self):
        """Success should reset failure count and close circuit"""
        cb = CircuitBreaker(failure_threshold=3)

        # Record some failures
        cb.record_failure()
        cb.record_failure()

        # Record success
        cb.record_success()

        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED

    def test_half_open_allows_one_request(self):
        """HALF_OPEN state should allow one test request"""
        cb = CircuitBreaker(failure_threshold=2, recovery_time=0)

        # Open the circuit
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # Force to half-open by setting last_failure_time to past
        cb.last_failure_time = time.time() - 100

        # Should allow execution in half-open
        assert cb.can_execute() is True
        assert cb.state == CircuitState.HALF_OPEN


class TestHuginnConfig:
    """Test cases for HuginnConfig dataclass"""

    def test_default_config_has_endpoints(self):
        """Default config should have both testnet and mainnet endpoints"""
        config = HuginnConfig()

        assert config.enabled is True
        assert config.check_interval == 3600
        assert config.timeout == 10
        assert isinstance(config.endpoints, dict)
        assert "testnet" in config.endpoints
        assert "mainnet" in config.endpoints

    def test_custom_endpoints(self):
        """Should allow custom endpoints"""
        config = HuginnConfig(
            endpoints={
                "testnet": "https://custom-testnet.example.com/api",
                "mainnet": "https://custom-mainnet.example.com/api",
            }
        )

        assert config.endpoints["testnet"] == "https://custom-testnet.example.com/api"
        assert config.endpoints["mainnet"] == "https://custom-mainnet.example.com/api"

    def test_backward_compatible_single_url(self):
        """Should support legacy base_url for backward compatibility"""
        config = HuginnConfig(base_url="https://legacy.example.com/api")

        # Should use base_url as testnet endpoint
        assert config.get_endpoint("testnet") == "https://legacy.example.com/api"
        assert config.get_endpoint("mainnet") == "https://legacy.example.com/api"

    def test_get_endpoint_testnet(self):
        """get_endpoint should return testnet URL"""
        config = HuginnConfig()

        assert config.get_endpoint("testnet") == TESTNET_API

    def test_get_endpoint_mainnet(self):
        """get_endpoint should return mainnet URL"""
        config = HuginnConfig()

        assert config.get_endpoint("mainnet") == MAINNET_API

    def test_get_endpoint_unknown_defaults_to_testnet(self):
        """Unknown network should default to testnet"""
        config = HuginnConfig()

        assert config.get_endpoint("unknown") == TESTNET_API
        assert config.get_endpoint(None) == TESTNET_API
        assert config.get_endpoint("") == TESTNET_API


class TestHuginnClientMultiNetwork:
    """Test cases for multi-network HuginnClient"""

    @pytest.fixture
    def multi_network_config(self):
        """Create config with both testnet and mainnet endpoints"""
        return HuginnConfig(
            endpoints={
                "testnet": TESTNET_API,
                "mainnet": MAINNET_API,
            },
            check_interval=3600,
            timeout=10,
        )

    @pytest.fixture
    def client(self, multi_network_config):
        """Create HuginnClient with multi-network config"""
        return HuginnClient(config=multi_network_config)

    def test_client_uses_testnet_endpoint(self, client):
        """Client should route to testnet endpoint when network=testnet"""
        secp = "0x1234567890abcdef"

        with responses.RequestsMock() as rsps:
            # Mock the target validator
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is True
            assert result.total_events == 1500

    def test_client_uses_mainnet_endpoint(self, client):
        """Client should route to mainnet endpoint when network=mainnet"""
        secp = "0xabcdef1234567890"

        with responses.RequestsMock() as rsps:
            # Mock the target validator
            rsps.add(
                responses.GET,
                f"{MAINNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            result = client.get_validator_uptime(secp, network="mainnet")

            assert result is not None
            assert result.is_active is True

    def test_client_default_network_is_testnet(self, client):
        """Client should default to testnet when network not specified"""
        secp = "0xdefaultnetwork"

        with responses.RequestsMock() as rsps:
            # Mock the target validator
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            result = client.get_validator_uptime(secp)  # No network param

            assert result is not None

    def test_per_network_caching(self, client):
        """Cache should be per (network, secp_address) tuple"""
        secp = "0xsameaddress"

        with responses.RequestsMock() as rsps:
            # Mock both endpoints with different responses
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json={**SAMPLE_ACTIVE_VALIDATOR_RESPONSE, "total_events": 100},
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{MAINNET_API}/validators/uptime/{secp}?period=all",
                json={**SAMPLE_ACTIVE_VALIDATOR_RESPONSE, "total_events": 200},
                status=200,
            )

            # Fetch from testnet
            testnet_result = client.get_validator_uptime(secp, network="testnet")
            assert testnet_result.total_events == 100

            # Fetch from mainnet - should be different
            mainnet_result = client.get_validator_uptime(secp, network="mainnet")
            assert mainnet_result.total_events == 200

            # Fetch testnet again - should be cached (100, not new value)
            testnet_cached = client.get_validator_uptime(secp, network="testnet")
            assert testnet_cached.total_events == 100

    def test_inactive_validator_detection(self, client):
        """Validator with status=inactive should be marked inactive"""
        secp = "0xinactive"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_INACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is False
            assert result.total_events == 0

    def test_active_validator_detection(self, client):
        """Validator with status=active should be marked active"""
        secp = "0xactive"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is True
            assert result.total_events > 0

    def test_rate_limit_returns_cached_data(self, client):
        """Rate limit (429) should return cached data if available"""
        secp = "0xratelimit"

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            # First request succeeds
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            # Get initial data
            result1 = client.get_validator_uptime(secp, network="testnet")
            assert result1 is not None

        # Clear cache time to force refresh
        cache_key = f"testnet:{secp.lower()}"
        client._cache_times[cache_key] = 0

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            # Second request gets rate limited
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json={"error": "rate limited"},
                status=429,
            )

            # Should return cached data
            result2 = client.get_validator_uptime(secp, network="testnet")
            assert result2 is not None
            assert result2.total_events == 1500

    def test_network_error_returns_cached_data(self, client):
        """Network error should return cached data if available"""
        secp = "0xnetworkerror"

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            # First request succeeds
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            result1 = client.get_validator_uptime(secp, network="testnet")
            assert result1 is not None

        # Clear cache time to force refresh
        cache_key = f"testnet:{secp.lower()}"
        client._cache_times[cache_key] = 0

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            # Second request fails with connection error
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                body=responses.ConnectionError("Network error"),
            )

            # Should return cached data
            result2 = client.get_validator_uptime(secp, network="testnet")
            assert result2 is not None

    def test_cache_validity_period(self, client):
        """Cache should be valid for check_interval seconds"""
        secp = "0xcachetest"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            # First call
            result1 = client.get_validator_uptime(secp, network="testnet")
            assert result1 is not None

            # Second call within interval - should use cache
            result2 = client.get_validator_uptime(secp, network="testnet")
            assert result2 is not None
            # Same fetched_at means it came from cache
            assert result1.fetched_at == result2.fetched_at

    def test_is_validator_active_wrapper(self, client):
        """is_validator_active should return boolean"""
        secp = "0xactivewrapper"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            is_active = client.is_validator_active(secp, network="testnet")
            assert is_active is True

    def test_empty_secp_returns_none(self, client):
        """Empty secp address should return None"""
        result = client.get_validator_uptime("", network="testnet")
        assert result is None

        result = client.get_validator_uptime(None, network="testnet")
        assert result is None

    def test_circuit_breaker_integration(self, client):
        """Circuit breaker should open after repeated failures"""
        secp = "0xcircuitbreaker"

        # Clear any existing circuit breaker
        client._circuit_breakers.clear()

        with responses.RequestsMock() as rsps:
            # Don't mock anything - all requests will fail
            # Make multiple calls to trigger circuit breaker
            for _ in range(6):
                client.get_validator_uptime(secp, network="testnet")

        # Check circuit breaker is open
        cb_status = client.get_circuit_breaker_status("testnet")
        assert cb_status["is_open"] is True


class TestValidatorUptime:
    """Test cases for ValidatorUptime dataclass"""

    def test_to_dict_serialization(self):
        """ValidatorUptime should serialize to dict correctly"""
        uptime = ValidatorUptime(
            validator_id=42,
            validator_name="Test",
            secp_address="0x1234",
            is_active=True,
            is_ever_active=True,
            uptime_percent=99.5,
            finalized_count=1000,
            timeout_count=5,
            total_events=1005,
            last_round=100,
            last_block_height=1000,
            since_utc="2024-01-01T00:00:00Z",
            fetched_at=1704067200.0,
        )

        result = uptime.to_dict()

        assert isinstance(result, dict)
        assert result["validator_id"] == 42
        assert result["is_active"] is True
        assert result["is_ever_active"] is True
        assert result["uptime_percent"] == 99.5
        assert result["fetched_at"] == 1704067200.0

    def test_uptime_percent_calculation(self):
        """Uptime percentage should be calculated correctly"""
        # This is tested via the client, but we verify the dataclass accepts it
        uptime = ValidatorUptime(
            validator_id=1,
            validator_name="Test",
            secp_address="0x1234",
            is_active=True,
            is_ever_active=True,
            uptime_percent=99.5,  # 1990/2000 * 100
            finalized_count=1990,
            timeout_count=10,
            total_events=2000,
            last_round=None,
            last_block_height=None,
            since_utc=None,
            fetched_at=time.time(),
        )

        assert uptime.uptime_percent == 99.5


class TestHuginnClientCacheOperations:
    """Test cases for cache management operations"""

    @pytest.fixture
    def client(self):
        """Create HuginnClient for testing"""
        return HuginnClient(config=HuginnConfig())

    def test_clear_cache(self, client):
        """clear_cache should remove all cached data"""
        secp = "0xcacheclear"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            client.get_validator_uptime(secp, network="testnet")

        # Verify cache has data
        assert len(client._cache) > 0

        # Clear cache
        client.clear_cache()

        # Verify cache is empty
        assert len(client._cache) == 0
        assert len(client._cache_times) == 0

    def test_get_cache_age(self, client):
        """get_cache_age should return age in seconds"""
        secp = "0xcacheage"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            client.get_validator_uptime(secp, network="testnet")

        age = client.get_cache_age(secp, network="testnet")
        assert age is not None
        assert age >= 0
        assert age < 5  # Should be very recent

    def test_get_cache_age_not_cached(self, client):
        """get_cache_age should return None for uncached addresses"""
        age = client.get_cache_age("0xnotcached", network="testnet")
        assert age is None


class TestStatusFieldDetection:
    """Test cases for active set detection from API status field only

    Active set status is determined solely from the Huginn API's "status" field.
    When the status field is missing, is_active is None, which triggers
    gmonads fallback in metrics.py.
    """

    @pytest.fixture
    def client(self):
        """Create HuginnClient for testing"""
        return HuginnClient(config=HuginnConfig())

    def test_active_from_status_field(self, client):
        """When API returns status='active', is_active should be True"""
        secp = "0xstatusactive"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,  # has "status": "active"
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is True

    def test_inactive_from_status_field(self, client):
        """When API returns status='inactive', is_active should be False"""
        secp = "0xstatusinactive"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_INACTIVE_VALIDATOR_RESPONSE,  # has "status": "inactive"
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is False

    def test_none_when_status_missing(self, client):
        """When API response has no status field, is_active should be None (triggers gmonads fallback)"""
        secp = "0xnostatus"
        client._circuit_breakers.clear()

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            # Mock target validator WITHOUT status field
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json={
                    "validator_id": 99,
                    "validator_name": "No Status Val",
                    "secp_address": secp,
                    # No "status" field
                    "finalized_count": 50,
                    "timeout_count": 0,
                    "total_events": 100,
                    "last_round": None,
                    "last_block_height": None,
                    "since_utc": "2024-01-01T00:00:00Z",
                },
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_ever_active is True
            # No status field = None (triggers gmonads fallback in metrics.py)
            assert result.is_active is None


class TestHuginnV2EndpointAndStatus:
    """Huginn API v2: canonical plural path, period=all, tri-state status"""

    @pytest.fixture
    def client(self):
        """Create HuginnClient for testing"""
        return HuginnClient(config=HuginnConfig())

    def test_canonical_path_with_cumulative_period(self, client):
        """Client must call /validators/uptime/{secp}?period=all (v2 canonical)"""
        secp = "0xcanonical"

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert len(rsps.calls) >= 1
            requested = rsps.calls[0].request.url
            assert requested == f"{TESTNET_API}/validators/uptime/{secp}?period=all"

    def test_pending_status_not_active_but_ever_active(self, client):
        """status=pending -> is_active False, is_ever_active True (has totals)"""
        secp = "0xpending"

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_PENDING_VALIDATOR_RESPONSE,
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is False
            assert result.is_ever_active is True
            assert result.total_events == 2000

    def test_unknown_status_value_falls_back_to_none(self, client):
        """Present-but-unexpected status value -> is_active None (gmonads fallback)"""
        secp = "0xunknownstatus"

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json={**SAMPLE_ACTIVE_VALIDATOR_RESPONSE, "status": "suspended"},
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is None
            assert result.is_ever_active is True


class TestHuginnV2HealthMerge:
    """Huginn API v2: /health best-effort merge into ValidatorUptime"""

    @pytest.fixture
    def client(self):
        """Create HuginnClient for testing"""
        return HuginnClient(config=HuginnConfig())

    def test_health_fields_merged(self, client):
        """last_round/last_block_height restored + liveness fields from /health"""
        secp = "0xhealthmerge"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/{secp}/health",
                json=SAMPLE_HEALTH_RESPONSE,
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is True
            # Fields removed from v2 uptime payload, restored from /health
            assert result.last_round == 51712837
            assert result.last_block_height == 12345678
            # New liveness fields
            assert result.uptime_24h == 99.9
            assert result.health_state == "healthy"
            assert result.seconds_since_last_event == 6
            assert result.last_event_utc == "2026-09-09T15:41:28.000Z"
            # Serialized too (dashboard/report consumers)
            d = result.to_dict()
            assert d["last_round"] == 51712837
            assert d["uptime_24h"] == 99.9
            assert d["health_state"] == "healthy"

    def test_health_404_keeps_uptime_result(self, client):
        """Health 404 must not drop a valid uptime verdict (best-effort merge)"""
        secp = "0xhealth404"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/{secp}/health",
                json={"success": False, "error": "Validator not found"},
                status=404,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is True
            assert result.last_round is None
            assert result.uptime_24h is None

    def test_health_merged_result_is_cached_as_one(self, client):
        """Composite result cached: second call within interval fetches nothing"""
        secp = "0xhealthcached"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/{secp}/health",
                json=SAMPLE_HEALTH_RESPONSE,
                status=200,
            )

            result1 = client.get_validator_uptime(secp, network="testnet")
            result2 = client.get_validator_uptime(secp, network="testnet")

            assert result1 is not None
            # Cache hit returns the same composite object - no refetch
            assert result2 is result1
            assert result1.last_round == 51712837


class TestHuginnFreshnessGate:
    """Huginn API v2: /status freshness gating of inactive verdicts"""

    @pytest.fixture
    def client(self):
        """Create HuginnClient for testing"""
        return HuginnClient(config=HuginnConfig())

    def test_stale_data_downgrades_inactive_verdict(self, client):
        """Stale Huginn data + inactive verdict -> is_active None (gmonads fallback)"""
        secp = "0xstaleinactive"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_INACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/status",
                json=SAMPLE_STATUS_STALE_RESPONSE,  # 600s since last raw round event
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is None

    def test_fresh_data_keeps_inactive_verdict(self, client):
        """Fresh Huginn data + inactive verdict stays False (real inactivity)"""
        secp = "0xfreshinactive"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_INACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/status",
                json=SAMPLE_STATUS_FRESH_RESPONSE,  # 5s since last raw round event
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is False

    def test_stale_data_keeps_active_verdict(self, client):
        """Stale data must not downgrade an active verdict (conservative)"""
        secp = "0xstaleactive"

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/status",
                json=SAMPLE_STATUS_STALE_RESPONSE,
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is True

    def test_status_failure_fails_open(self, client):
        """/status unavailable -> verdicts are NOT downgraded (fail-open)"""
        secp = "0xstatusdown"

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_INACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            # /status intentionally NOT mocked -> request error -> fail-open

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is False

    def test_freshness_polled_at_most_once_per_interval(self, client, monkeypatch):
        """/status is polled once per FRESHNESS_CHECK_INTERVAL per network"""
        monkeypatch.setattr("monad_monitor.huginn.FRESHNESS_CHECK_INTERVAL", 3600)
        secp = "0xfreshcached"
        # check_interval=0 forces an uptime refetch on every call, so a second
        # call proves /status is served from its own cache, not refetched.
        client.config.check_interval = 0

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_INACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_INACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/status",
                json=SAMPLE_STATUS_FRESH_RESPONSE,
                status=200,
            )

            client.get_validator_uptime(secp, network="testnet")
            client.get_validator_uptime(secp, network="testnet")

            status_calls = [c for c in rsps.calls if c.request.url.endswith("/status")]
            assert len(status_calls) == 1


class TestUptime30dWindow:
    """Huginn API v2: 30d cumulative window merged beside the all-time totals"""

    @pytest.fixture
    def client(self):
        return HuginnClient(config=HuginnConfig())

    def test_30d_window_merged_without_touching_all_time(self, client):
        secp = "0xwindow30d"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=30d",
                json=SAMPLE_UPTIME_30D_RESPONSE,
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            # All-time figures keep their own values
            assert result.uptime_percent == 100.0
            assert result.total_events == 1500
            # ...and the 30d window rides alongside
            assert result.uptime_30d == 99.63
            assert result.finalized_count_30d == 32300
            assert result.timeout_count_30d == 119
            assert result.total_events_30d == 32419

    def test_30d_window_in_serialized_payload(self, client):
        secp = "0xwindow30d"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=30d",
                json=SAMPLE_UPTIME_30D_RESPONSE,
                status=200,
            )

            payload = client.get_validator_uptime(secp, network="testnet").to_dict()

            assert payload["uptime_30d"] == 99.63
            assert payload["timeout_count_30d"] == 119
            assert payload["total_events_30d"] == 32419

    def test_30d_window_failure_leaves_other_fields_intact(self, client):
        secp = "0xwindowfail"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            # /30d intentionally NOT mocked -> request error -> window unknown

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.uptime_30d is None
            assert result.total_events_30d is None
            assert result.uptime_percent == 100.0
            assert result.is_active is True

    def test_30d_window_without_events_reports_no_data(self, client):
        secp = "0xwindownoevents"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=all",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators/uptime/{secp}?period=30d",
                json={
                    "success": True,
                    "period": "30d",
                    "uptime": {
                        "validator_id": 42,
                        "status": "inactive",
                        "finalized_count": 0,
                        "timeout_count": 0,
                        "total_events": 0,
                    },
                },
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            # No events must not be rendered as 0% uptime
            assert result.uptime_30d is None
            assert result.total_events_30d == 0


class TestValidatorSet:
    """Huginn staking API: next-epoch exit detection"""

    @pytest.fixture
    def client(self):
        return HuginnClient(config=HuginnConfig())

    @pytest.fixture
    def validator_set_url(self):
        return f"{TESTNET_API}/staking/validator-set"

    def test_leaving_ids_are_detected(self, client, validator_set_url):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                validator_set_url,
                json=SAMPLE_VALIDATOR_SET_RESPONSE,
                status=200,
            )

            state = client.get_validator_set("testnet")

            assert state.epoch == 1233
            assert state.in_delay_period is False
            assert state.counts["leaving"] == 2
            assert state.is_leaving(67) is True
            assert state.is_leaving(224) is False
            assert state.is_leaving(None) is False

    def test_entering_ids_are_detected(self, client, validator_set_url):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                validator_set_url,
                json=SAMPLE_VALIDATOR_SET_RESPONSE,
                status=200,
            )

            state = client.get_validator_set("testnet")

            assert state.entering_ids == {231}
            assert state.entering[0]["name"] == "Example"
            assert state.is_entering(231) is True
            assert state.is_entering(67) is False
            assert state.is_entering(None) is False

    def test_validator_set_cached_per_network(self, client, validator_set_url):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                validator_set_url,
                json=SAMPLE_VALIDATOR_SET_RESPONSE,
                status=200,
            )

            first = client.get_validator_set("testnet")
            second = client.get_validator_set("testnet")

            assert first is second
            assert len(rsps.calls) == 1

    def test_validator_set_failure_keeps_last_known_state(self, client, validator_set_url):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                validator_set_url,
                json=SAMPLE_VALIDATOR_SET_RESPONSE,
                status=200,
            )
            client.get_validator_set("testnet")

        client.config.check_interval = 0  # force a refresh attempt
        with responses.RequestsMock():
            # /staking/validator-set intentionally NOT mocked -> request error
            state = client.get_validator_set("testnet")

        assert state is not None
        assert state.epoch == 1233

    def test_validator_set_failure_without_cache_is_none(self, client):
        with responses.RequestsMock():
            assert client.get_validator_set("testnet") is None

    def test_secp_to_id_map_paginates_and_caches(self, client):
        secp_a = "0xaaa"
        secp_b = "0xbbb"
        secp_c = "0xccc"

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators?limit=500&offset=0",
                json={
                    "success": True,
                    "count": 2,
                    "total": 3,
                    "validators": [
                        {"id": 1, "secp_address": secp_a},
                        {"id": 2, "secp_address": secp_b},
                    ],
                },
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validators?limit=500&offset=2",
                json={
                    "success": True,
                    "count": 1,
                    "total": 3,
                    "validators": [{"id": 3, "secp_address": secp_c}],
                },
                status=200,
            )

            assert client.get_validator_id(secp_b, network="testnet") == 2
            # Remaining ids come from the cached map, not a third page request
            assert client.get_validator_id(secp_c, network="testnet") == 3
            assert client.get_validator_id("0xunknown", network="testnet") is None
            assert len(rsps.calls) == 2

    def test_validator_id_without_secp_is_none(self, client):
        assert client.get_validator_id(None, network="testnet") is None
        assert client.get_validator_id("", network="testnet") is None
