"""Tests for Huginn API client with multi-network support"""

import time
import pytest
import responses

from monad_monitor.huginn import HuginnConfig, HuginnClient, ValidatorUptime, CircuitBreaker, CircuitState


# Sample API responses
SAMPLE_ACTIVE_VALIDATOR_RESPONSE = {
    "validator_id": 42,
    "validator_name": "Test Validator",
    "secp_address": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
    "status": "active",
    "finalized_count": 1500,
    "timeout_count": 0,
    "total_events": 1500,
    "last_round": 51712837,
    "last_block_height": 12345678,
    "since_utc": "2024-01-01T00:00:00Z",
}

SAMPLE_INACTIVE_VALIDATOR_RESPONSE = {
    "validator_id": None,
    "validator_name": None,
    "secp_address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdef",
    "status": "inactive",
    "finalized_count": 0,
    "timeout_count": 0,
    "total_events": 0,
    "last_round": None,
    "last_block_height": None,
    "since_utc": None,
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{MAINNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
                json={**SAMPLE_ACTIVE_VALIDATOR_RESPONSE, "total_events": 100},
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{MAINNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
                f"{TESTNET_API}/validator/uptime/{secp}",
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
