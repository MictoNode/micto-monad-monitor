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
    "finalized_count": 0,
    "timeout_count": 0,
    "total_events": 0,  # Zero events = inactive
    "last_round": None,
    "last_block_height": None,
    "since_utc": None,
}

# Reference validator response (for network round)
SAMPLE_REFERENCE_VALIDATOR_RESPONSE = {
    "validator_id": 1,
    "validator_name": "Monad Foundation",
    "finalized_count": 10000,
    "timeout_count": 0,
    "total_events": 10000,
    "last_round": 51712837,
    "last_block_height": 12345678,
    "since_utc": "2024-01-01T00:00:00Z",
}

# Endpoint URLs
TESTNET_API = "https://validator-api-testnet.huginn.tech/monad-api"
MAINNET_API = "https://validator-api.huginn.tech/monad-api"


def mock_reference_validators(rsps, base_url, network_round=51712837):
    """
    Helper to mock reference validator responses for multi-validator strategy.

    The client queries TOP 5 validators by stake (IDs 1-5) to get network round.
    We mock at least one successful response.
    """
    response = {
        "success": True,
        "uptime": {**SAMPLE_REFERENCE_VALIDATOR_RESPONSE, "last_round": network_round}
    }
    # Mock all 5 reference validators (IDs 1-5)
    for ref_id in range(1, 6):
        rsps.add(
            responses.GET,
            f"{base_url}/validator/uptime/{ref_id}",
            json=response,
            status=200,
        )


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
            # Mock reference validators for network round
            mock_reference_validators(rsps, TESTNET_API)

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
            # Mock reference validators for mainnet
            mock_reference_validators(rsps, MAINNET_API)

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
            # Mock reference validators for testnet
            mock_reference_validators(rsps, TESTNET_API)

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
            # Mock reference validators for both networks
            mock_reference_validators(rsps, TESTNET_API, network_round=100)
            mock_reference_validators(rsps, MAINNET_API, network_round=200)

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
        """Validator with total_events=0 should be marked inactive"""
        secp = "0xinactive"

        with responses.RequestsMock() as rsps:
            # Mock reference validators
            mock_reference_validators(rsps, TESTNET_API)

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
        """Validator with total_events>0 should be marked active"""
        secp = "0xactive"

        with responses.RequestsMock() as rsps:
            # Mock reference validators
            mock_reference_validators(rsps, TESTNET_API)

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
            # Mock reference validators
            mock_reference_validators(rsps, TESTNET_API)

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
            # Mock reference validators
            mock_reference_validators(rsps, TESTNET_API)

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
            # Mock reference validators
            mock_reference_validators(rsps, TESTNET_API)

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
            # Mock reference validators
            mock_reference_validators(rsps, TESTNET_API)

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
            # Mock reference validators
            mock_reference_validators(rsps, TESTNET_API)

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
            # Mock reference validators
            mock_reference_validators(rsps, TESTNET_API)

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
            # Mock reference validators
            mock_reference_validators(rsps, TESTNET_API)

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
            # Mock reference validators
            mock_reference_validators(rsps, TESTNET_API)

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


class TestGmonadsBackedValidatorSelection:
    """Test cases for gmonads-backed active validator selection (rate limit optimization)"""

    @pytest.fixture
    def huginn_client(self):
        """Create HuginnClient for testing"""
        return HuginnClient(config=HuginnConfig())

    @pytest.fixture
    def gmonads_client(self):
        """Create GmonadsClient for testing"""
        from monad_monitor.gmonads import GmonadsConfig, GmonadsClient
        return GmonadsClient(config=GmonadsConfig())

    def test_uses_gmonads_active_validator_when_available(self, huginn_client, gmonads_client):
        """When gmonads provides an active validator, should query only that single validator from Huginn"""
        secp = "0xgmonadsactive"

        with responses.RequestsMock() as rsps:
            # Mock gmonads to return validators with active one having val_index=42
            rsps.add(
                responses.GET,
                "https://www.gmonads.com/api/v1/public/validators/epoch",
                json={
                    "success": True,
                    "data": [
                        {"node_id": "02abc", "val_index": 42, "stake": "1000000", "commission": 0.05, "validator_set_type": "active"},
                        {"node_id": "03def", "val_index": 10, "stake": "500000", "commission": 0.10, "validator_set_type": "inactive"},
                    ],
                },
                status=200,
            )

            # Mock Huginn - only validator ID 42 should be queried (the active one from gmonads)
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/42",
                json={"success": True, "uptime": {"last_round": 51712837}},
                status=200,
            )

            # Mock target validator
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/{secp}",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            result = huginn_client.get_validator_uptime(secp, network="testnet", gmonads_client=gmonads_client)

            assert result is not None
            assert result.current_network_round == 51712837
            # Verify only 1 Huginn call was made for network round (not 5)
            # The responses library will fail if more calls are made than mocked

    def test_fallback_to_multi_validator_when_gmonads_fails(self, huginn_client, gmonads_client):
        """When gmonads fails, should fallback to multi-validator strategy"""
        secp = "0xfallbacktest"

        with responses.RequestsMock() as rsps:
            # Mock gmonads to fail
            rsps.add(
                responses.GET,
                "https://www.gmonads.com/api/v1/public/validators/epoch",
                body=responses.ConnectionError("gmonads down"),
            )

            # Mock reference validators (multi-validator fallback)
            mock_reference_validators(rsps, TESTNET_API, network_round=51712837)

            # Mock target validator
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/{secp}",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            result = huginn_client.get_validator_uptime(secp, network="testnet", gmonads_client=gmonads_client)

            assert result is not None
            assert result.current_network_round == 51712837

    def test_fallback_to_multi_validator_when_no_active_validators(self, huginn_client, gmonads_client):
        """When gmonads returns no active validators, should fallback to multi-validator strategy"""
        secp = "0xnoactive"

        with responses.RequestsMock() as rsps:
            # Mock gmonads to return only inactive validators
            rsps.add(
                responses.GET,
                "https://www.gmonads.com/api/v1/public/validators/epoch",
                json={
                    "success": True,
                    "data": [
                        {"node_id": "02abc", "val_index": 1, "stake": "1000000", "commission": 0.05, "validator_set_type": "inactive"},
                    ],
                },
                status=200,
            )

            # Mock reference validators (multi-validator fallback)
            mock_reference_validators(rsps, TESTNET_API, network_round=51712837)

            # Mock target validator
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/{secp}",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            result = huginn_client.get_validator_uptime(secp, network="testnet", gmonads_client=gmonads_client)

            assert result is not None
            assert result.current_network_round == 51712837

    def test_no_gmonads_client_uses_multi_validator(self, huginn_client):
        """When gmonads_client is not provided, should use multi-validator strategy"""
        secp = "0xnogmonads"

        with responses.RequestsMock() as rsps:
            # Mock reference validators (multi-validator strategy)
            mock_reference_validators(rsps, TESTNET_API, network_round=51712837)

            # Mock target validator
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/{secp}",
                json=SAMPLE_ACTIVE_VALIDATOR_RESPONSE,
                status=200,
            )

            # Call without gmonads_client
            result = huginn_client.get_validator_uptime(secp, network="testnet", gmonads_client=None)

            assert result is not None
            assert result.current_network_round == 51712837


class TestMultiValidatorStrategy:
    """Test cases for multi-validator network round strategy"""

    @pytest.fixture
    def client(self):
        """Create HuginnClient for testing"""
        return HuginnClient(config=HuginnConfig())

    def test_uses_max_round_from_reference_validators(self, client):
        """Should use MAX round from successful reference validators"""
        secp = "0xmaxroundtest"

        with responses.RequestsMock() as rsps:
            # Mock reference validators with different rounds
            # ID 1: round 100
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/1",
                json={"success": True, "uptime": {"last_round": 100}},
                status=200,
            )
            # ID 2: round 150 (MAX)
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/2",
                json={"success": True, "uptime": {"last_round": 150}},
                status=200,
            )
            # ID 3: round 120
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/3",
                json={"success": True, "uptime": {"last_round": 120}},
                status=200,
            )
            # ID 4-5: fail
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/4",
                json={"error": "not found"},
                status=404,
            )
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/5",
                json={"error": "not found"},
                status=404,
            )

            # Mock target validator with round 145 (within threshold of 150)
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/{secp}",
                json={**SAMPLE_ACTIVE_VALIDATOR_RESPONSE, "last_round": 145},
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.current_network_round == 150
            assert result.round_diff == 5  # 150 - 145

    def test_fallback_to_cached_round_on_all_failures(self, client):
        """Should use cached round when all reference validators fail"""
        secp = "0xfallbacktest"

        # Clear circuit breaker to ensure test starts fresh
        client._circuit_breakers.clear()

        # Set up a cached round
        client._network_rounds["testnet"] = 500
        client._network_round_times["testnet"] = time.time() - 1000  # Old cache

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            # All reference validators fail with 404 (not retried, doesn't trigger circuit breaker)
            for ref_id in range(1, 6):
                rsps.add(
                    responses.GET,
                    f"{TESTNET_API}/validator/uptime/{ref_id}",
                    json={"error": "not found"},
                    status=404,
                )

            # Mock target validator
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/{secp}",
                json={**SAMPLE_ACTIVE_VALIDATOR_RESPONSE, "last_round": 450},
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            # Should use cached round (500)
            assert result is not None
            assert result.current_network_round == 500


class TestIsActiveFallbackSafety:
    """Test cases for safe fallback when network round unavailable (Season 5.2)

    When network round cannot be fetched, the system should NOT assume is_active=True.
    Instead, it should use a more conservative approach with confidence indicators.
    """

    @pytest.fixture
    def client(self):
        """Create HuginnClient for testing"""
        return HuginnClient(config=HuginnConfig())

    def test_unknown_active_status_when_no_network_round(self, client):
        """When network round unavailable, confidence should be 'unknown' not assumed active"""
        secp = "0xunknownstatus"
        client._circuit_breakers.clear()

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            # All reference validators fail with 404 (not retried, doesn't trigger circuit breaker)
            for ref_id in range(1, 6):
                rsps.add(
                    responses.GET,
                    f"{TESTNET_API}/validator/uptime/{ref_id}",
                    json={"error": "not found"},
                    status=404,
                )

            # No cached network round

            # Mock target validator that has been active before
            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/{secp}",
                json={**SAMPLE_ACTIVE_VALIDATOR_RESPONSE, "total_events": 100},
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_ever_active is True
            # When network round is unavailable, should NOT assume active
            assert result.is_active is False  # Conservative default
            assert result.confidence == "unknown"  # Indicates uncertainty
            assert result.round_diff is None

    def test_high_confidence_when_round_available(self, client):
        """When network round is available, confidence should be 'high'"""
        secp = "0xhighconf"
        client._circuit_breakers.clear()

        with responses.RequestsMock() as rsps:
            mock_reference_validators(rsps, TESTNET_API, network_round=1000)

            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/{secp}",
                json={**SAMPLE_ACTIVE_VALIDATOR_RESPONSE, "last_round": 990},
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.is_active is True
            assert result.confidence == "high"
            assert result.round_diff == 10

    def test_uses_cached_round_with_medium_confidence(self, client):
        """When using cached round, confidence should be 'medium'"""
        secp = "0xcachedconf"
        client._circuit_breakers.clear()

        # Set up cached round
        client._network_rounds["testnet"] = 2000
        client._network_round_times["testnet"] = time.time() - 200  # Slightly old

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            # All reference validators fail
            for ref_id in range(1, 6):
                rsps.add(
                    responses.GET,
                    f"{TESTNET_API}/validator/uptime/{ref_id}",
                    json={"error": "unavailable"},
                    status=500,
                )

            rsps.add(
                responses.GET,
                f"{TESTNET_API}/validator/uptime/{secp}",
                json={**SAMPLE_ACTIVE_VALIDATOR_RESPONSE, "last_round": 1990},
                status=200,
            )

            result = client.get_validator_uptime(secp, network="testnet")

            assert result is not None
            assert result.current_network_round == 2000  # From cache
            assert result.confidence == "medium"  # Cached data
            assert result.is_active is True  # Can determine from cached round
