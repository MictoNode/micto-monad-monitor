"""Tests for gmonads API client"""

import time
import pytest
import responses

from monad_monitor.gmonads import (
    GmonadsConfig,
    GmonadsClient,
    EpochValidator,
    BlockMetrics,
    BlockMetricsTrend,
    NetworkHealth,
    decompress_public_key,
    compress_public_key,
    public_keys_match,
)


# Sample API responses (matching actual gmonads.com API structure)
SAMPLE_EPOCH_VALIDATORS = {
    "success": True,
    "data": [
        {
            "node_id": "0203a26b820dafdb794f1fc7117ba8e897830b184dcb08e45a44262f018deabaf3",
            "val_index": 1,
            "stake": "10000000",
            "commission": 0.05,
            "validator_set_type": "active",
        },
        {
            "node_id": "0262ad33b9e77584122b1d308a4fbd72f00d54e6733ba51d23b31d4758455b71c",
            "val_index": 2,
            "stake": "5000000",
            "commission": 0.10,
            "validator_set_type": "active",
        },
        {
            "node_id": "039999999999999999999999999999999999999999999999999999999999999999",
            "val_index": 3,
            "stake": "1000000",
            "commission": 0.15,
            "validator_set_type": "inactive",
        },
    ],
    "meta": {"total": 3}
}

SAMPLE_BLOCK_METRICS = {
    "success": True,
    "data": [
        {
            "bucket": "2026-02-24T16:24:00.000Z",
            "blocks": "100",
            "txs": "7550",
            "avg_tps": 75.5,
            "avg_block_fullness_pct": 65.2,
        },
        {
            "bucket": "2026-02-24T16:25:00.000Z",
            "blocks": "100",
            "txs": "7500",
            "avg_tps": 75.0,
            "avg_block_fullness_pct": 64.8,
        },
    ],
    "meta": {}
}

# Sample for trend testing (more buckets)
SAMPLE_BLOCK_METRICS_MANY_BUCKETS = {
    "success": True,
    "data": [
        # First 3 buckets = "previous" period
        {"bucket": "2026-02-24T16:20:00.000Z", "blocks": "100", "txs": "10000", "avg_tps": 100.0, "avg_block_fullness_pct": 55.0},
        {"bucket": "2026-02-24T16:21:00.000Z", "blocks": "100", "txs": "10000", "avg_tps": 100.0, "avg_block_fullness_pct": 55.0},
        {"bucket": "2026-02-24T16:22:00.000Z", "blocks": "100", "txs": "10000", "avg_tps": 100.0, "avg_block_fullness_pct": 55.0},
        # Last bucket = "recent" period
        {"bucket": "2026-02-24T16:23:00.000Z", "blocks": "100", "txs": "7000", "avg_tps": 70.0, "avg_block_fullness_pct": 60.0},
    ],
    "meta": {}
}

BASE_URL = "https://www.gmonads.com/api/v1/public"


class TestGmonadsConfig:
    """Test cases for GmonadsConfig dataclass"""

    def test_default_config(self):
        """Default config should have expected values"""
        config = GmonadsConfig()

        assert config.base_url == BASE_URL
        assert config.enabled is True
        assert config.check_interval == 120
        assert config.timeout == 10

    def test_custom_config(self):
        """Should allow custom values"""
        config = GmonadsConfig(
            base_url="https://custom.example.com/api",
            enabled=False,
            check_interval=300,
            timeout=20,
        )

        assert config.base_url == "https://custom.example.com/api"
        assert config.enabled is False
        assert config.check_interval == 300
        assert config.timeout == 20


class TestGmonadsClient:
    """Test cases for GmonadsClient"""

    @pytest.fixture
    def config(self):
        return GmonadsConfig()

    @pytest.fixture
    def client(self, config):
        return GmonadsClient(config=config)

    def test_get_epoch_validators_success(self, client):
        """Should fetch and parse epoch validators"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{BASE_URL}/validators/epoch",
                json=SAMPLE_EPOCH_VALIDATORS,
                status=200,
            )

            result = client.get_epoch_validators("testnet")

            assert result is not None
            assert len(result) == 3
            assert result[0].validator_set_type == "active"
            assert result[2].validator_set_type == "inactive"
            # Stake should be parsed from string
            assert result[0].stake == 10000000.0

    def test_get_epoch_validators_cached(self, client):
        """Should cache epoch validators"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{BASE_URL}/validators/epoch",
                json=SAMPLE_EPOCH_VALIDATORS,
                status=200,
            )

            # First call
            result1 = client.get_epoch_validators("testnet")
            assert result1 is not None

            # Second call should use cache (no new request)
            result2 = client.get_epoch_validators("testnet")
            assert result2 is not None
            # Same fetched_at means cached
            assert result1[0].fetched_at == result2[0].fetched_at

    def test_get_epoch_validators_error_returns_cached(self, client):
        """Should return cached data on error"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{BASE_URL}/validators/epoch",
                json=SAMPLE_EPOCH_VALIDATORS,
                status=200,
            )

            result1 = client.get_epoch_validators("testnet")
            assert result1 is not None

        # Clear cache time to force refresh
        client._validators_cache_times["testnet"] = 0

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{BASE_URL}/validators/epoch",
                body=responses.ConnectionError("Network error"),
            )

            result2 = client.get_epoch_validators("testnet")
            # Should return cached data
            assert result2 is not None

    def test_get_block_metrics_1m_success(self, client):
        """Should fetch and aggregate block metrics from buckets"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{BASE_URL}/blocks/1m",
                json=SAMPLE_BLOCK_METRICS,
                status=200,
            )

            result = client.get_block_metrics_1m("testnet")

            assert result is not None
            # Average of 75.5 and 75.0
            assert result.avg_tps == 75.25
            # Average of 65.2 and 64.8
            assert result.avg_block_fullness_pct == 65.0
            # Sum of blocks
            assert result.total_blocks == 200
            # Sum of txs
            assert result.total_txs == 15050

    def test_get_block_metrics_trend_success(self, client):
        """Should calculate trend from bucket data"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{BASE_URL}/blocks/1m",
                json=SAMPLE_BLOCK_METRICS_MANY_BUCKETS,
                status=200,
            )

            result = client.get_block_metrics_trend("testnet")

            assert result is not None
            # Recent = last bucket (70.0), Previous = first 3 buckets avg (100.0)
            assert result.current_tps == 70.0
            assert result.previous_tps == 100.0
            # TPS dropped 30%
            assert result.tps_change_percent == -30.0

    def test_is_validator_in_active_set_true(self, client):
        """Should return True for active validator (exact match)"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{BASE_URL}/validators/epoch",
                json=SAMPLE_EPOCH_VALIDATORS,
                status=200,
            )

            # Use exact node_id from sample data
            secp = "0203a26b820dafdb794f1fc7117ba8e897830b184dcb08e45a44262f018deabaf3"
            result = client.is_validator_in_active_set(secp, "testnet")

            assert result is True

    def test_is_validator_in_active_set_false(self, client):
        """Should return False for inactive validator"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{BASE_URL}/validators/epoch",
                json=SAMPLE_EPOCH_VALIDATORS,
                status=200,
            )

            # Use node_id of inactive validator
            secp = "039999999999999999999999999999999999999999999999999999999999999999"
            result = client.is_validator_in_active_set(secp, "testnet")

            assert result is False

    def test_is_validator_in_active_set_not_found(self, client):
        """Should return None for unknown validator"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{BASE_URL}/validators/epoch",
                json=SAMPLE_EPOCH_VALIDATORS,
                status=200,
            )

            secp = "000000000000000000000000000000000000000000000000000000000000000000"
            result = client.is_validator_in_active_set(secp, "testnet")

            assert result is None

    def test_get_active_validator_count(self, client):
        """Should count active validators"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{BASE_URL}/validators/epoch",
                json=SAMPLE_EPOCH_VALIDATORS,
                status=200,
            )

            count = client.get_active_validator_count("testnet")

            assert count == 2  # 2 active validators in sample

    def test_clear_cache(self, client):
        """Should clear all caches"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f"{BASE_URL}/validators/epoch",
                json=SAMPLE_EPOCH_VALIDATORS,
                status=200,
            )
            rsps.add(
                responses.GET,
                f"{BASE_URL}/blocks/1m",
                json=SAMPLE_BLOCK_METRICS,
                status=200,
            )

            client.get_epoch_validators("testnet")
            client.get_block_metrics_1m("testnet")

        # Verify caches have data
        assert len(client._validators_cache) > 0
        assert len(client._metrics_cache) > 0

        # Clear cache
        client.clear_cache()

        # Verify caches are empty
        assert len(client._validators_cache) == 0
        assert len(client._validators_cache_times) == 0
        assert len(client._metrics_cache) == 0
        assert len(client._metrics_cache_times) == 0


class TestDataclasses:
    """Test cases for dataclass serialization"""

    def test_epoch_validator_to_dict(self):
        """EpochValidator should serialize correctly"""
        v = EpochValidator(
            node_id="0x1234",
            val_index=1,
            stake=100.0,
            commission=0.05,
            validator_set_type="active",
            fetched_at=time.time(),
        )

        d = v.to_dict()

        assert d["node_id"] == "0x1234"
        assert d["val_index"] == 1
        assert d["stake"] == 100.0
        assert d["validator_set_type"] == "active"

    def test_block_metrics_to_dict(self):
        """BlockMetrics should serialize correctly"""
        m = BlockMetrics(
            avg_tps=50.0,
            avg_block_fullness_pct=60.0,
            total_blocks=100,
            total_txs=5000,
            fetched_at=time.time(),
        )

        d = m.to_dict()

        assert d["avg_tps"] == 50.0
        assert d["avg_block_fullness_pct"] == 60.0
        assert d["total_blocks"] == 100
        assert d["total_txs"] == 5000

    def test_block_metrics_trend_to_dict(self):
        """BlockMetricsTrend should serialize correctly"""
        t = BlockMetricsTrend(
            current_tps=70.0,
            previous_tps=100.0,
            tps_change_percent=-30.0,
            current_fullness=60.0,
            previous_fullness=55.0,
            fullness_change_percent=9.09,
        )

        d = t.to_dict()

        assert d["current_tps"] == 70.0
        assert d["previous_tps"] == 100.0
        assert d["tps_change_percent"] == -30.0

    def test_network_health_to_dict(self):
        """NetworkHealth should serialize correctly"""
        h = NetworkHealth(
            tps=50.0,
            tps_status="normal",
            block_fullness=60.0,
            active_validators=100,
            alerts=["test alert"],
            fetched_at=time.time(),
        )

        d = h.to_dict()

        assert d["tps"] == 50.0
        assert d["tps_status"] == "normal"
        assert d["active_validators"] == 100
        assert d["alerts"] == ["test alert"]


class TestPublicKeyConversion:
    """Test cases for public key conversion functions"""

    # Known test vectors
    COMPRESSED_KEY = "0203a26b820dafdb794f1fc7117ba8e897830b184dcb08e45a44262f018deabaf3"
    UNCOMPRESSED_KEY = "0403a26b820dafdb794f1fc7117ba8e897830b184dcb08e45a44262f018deabaf3a3304676e04db4b15df81e229434dd10370681713150f404104ceadb89560d1a"

    def test_decompress_public_key(self):
        """Should decompress a compressed public key"""
        result = decompress_public_key(self.COMPRESSED_KEY)

        assert result is not None
        assert result == self.UNCOMPRESSED_KEY
        assert len(result) == 130  # 04 + 64 + 64

    def test_compress_public_key(self):
        """Should compress an uncompressed public key"""
        result = compress_public_key(self.UNCOMPRESSED_KEY)

        assert result is not None
        assert result == self.COMPRESSED_KEY
        assert len(result) == 66  # 02/03 + 64

    def test_compress_without_prefix(self):
        """Should compress uncompressed key without 04 prefix"""
        key_without_prefix = self.UNCOMPRESSED_KEY[2:]  # Remove 04
        result = compress_public_key(key_without_prefix)

        assert result is not None
        assert result == self.COMPRESSED_KEY

    def test_roundtrip(self):
        """Decompress then compress should return original"""
        decompressed = decompress_public_key(self.COMPRESSED_KEY)
        recompressed = compress_public_key(decompressed)

        assert recompressed == self.COMPRESSED_KEY

    def test_public_keys_match_same_format(self):
        """Should match keys in same format"""
        assert public_keys_match(self.COMPRESSED_KEY, self.COMPRESSED_KEY)
        assert public_keys_match(self.UNCOMPRESSED_KEY, self.UNCOMPRESSED_KEY)

    def test_public_keys_match_different_formats(self):
        """Should match compressed with uncompressed"""
        assert public_keys_match(self.COMPRESSED_KEY, self.UNCOMPRESSED_KEY)
        assert public_keys_match(self.UNCOMPRESSED_KEY, self.COMPRESSED_KEY)

    def test_public_keys_match_with_0x_prefix(self):
        """Should handle 0x prefix"""
        compressed_0x = "0x" + self.COMPRESSED_KEY
        uncompressed_0x = "0x" + self.UNCOMPRESSED_KEY

        assert public_keys_match(compressed_0x, self.UNCOMPRESSED_KEY)
        assert public_keys_match(self.COMPRESSED_KEY, uncompressed_0x)
        assert public_keys_match(compressed_0x, uncompressed_0x)

    def test_public_keys_no_match(self):
        """Should return False for different keys"""
        different_compressed = "02" + "0" * 64
        assert not public_keys_match(self.COMPRESSED_KEY, different_compressed)

    def test_decompress_invalid_key(self):
        """Should return None for invalid keys"""
        assert decompress_public_key("") is None
        assert decompress_public_key("invalid") is None
        assert decompress_public_key("04" + "0" * 64) is None  # Wrong prefix for compressed

    def test_compress_invalid_key(self):
        """Should return None for invalid keys"""
        assert compress_public_key("") is None
        assert compress_public_key("invalid") is None
        assert compress_public_key("02" + "0" * 64) is None  # Wrong format for uncompressed
