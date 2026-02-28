"""gmonads.com API client for network-wide metrics and validator status"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

import requests

logger = logging.getLogger(__name__)


# Default API base URL
DEFAULT_BASE_URL = "https://www.gmonads.com/api/v1/public"


def decompress_public_key(compressed_key: str) -> Optional[str]:
    """
    Convert a compressed secp256k1 public key to uncompressed format.

    Args:
        compressed_key: Compressed public key (66 hex chars, starts with 02 or 03)

    Returns:
        Uncompressed public key (128 hex chars, without 04 prefix), or None on error
    """
    try:
        # Remove 0x prefix if present
        if compressed_key.startswith("0x"):
            compressed_key = compressed_key[2:]

        compressed_key = compressed_key.lower()

        # Validate compressed key format
        if len(compressed_key) != 66 or compressed_key[:2] not in ("02", "03"):
            return None

        # Use ecdsa library for decompression
        from ecdsa import VerifyingKey, SECP256k1

        # Create verifying key from compressed format
        vk = VerifyingKey.from_string(bytes.fromhex(compressed_key), curve=SECP256k1)

        # Get uncompressed string (64 bytes = 128 hex chars, without 04 prefix)
        uncompressed = vk.to_string("uncompressed").hex()

        return uncompressed

    except Exception as e:
        logger.warning(f"Error decompressing public key: {e}")
        return None


def compress_public_key(uncompressed_key: str) -> Optional[str]:
    """
    Convert an uncompressed secp256k1 public key to compressed format.

    Args:
        uncompressed_key: Uncompressed public key (128 hex chars, with or without 04 prefix)

    Returns:
        Compressed public key (66 hex chars, with 02 or 03 prefix), or None on error
    """
    try:
        # Remove 0x prefix if present
        if uncompressed_key.startswith("0x"):
            uncompressed_key = uncompressed_key[2:]

        uncompressed_key = uncompressed_key.lower()

        # Remove 04 prefix if present
        if uncompressed_key.startswith("04") and len(uncompressed_key) == 130:
            uncompressed_key = uncompressed_key[2:]

        # Validate uncompressed key format
        if len(uncompressed_key) != 128:
            return None

        # Use ecdsa library for compression
        from ecdsa import VerifyingKey, SECP256k1

        # Create verifying key from uncompressed format
        vk = VerifyingKey.from_string(bytes.fromhex(uncompressed_key), curve=SECP256k1)

        # Get compressed string (33 bytes = 66 hex chars)
        compressed = vk.to_string("compressed").hex()

        return compressed

    except Exception as e:
        logger.warning(f"Error compressing public key: {e}")
        return None


def public_keys_match(key1: str, key2: str) -> bool:
    """
    Check if two public keys represent the same key (handles format differences).

    Args:
        key1: First public key (compressed or uncompressed)
        key2: Second public key (compressed or uncompressed)

    Returns:
        True if they represent the same key, False otherwise
    """
    # Normalize keys
    for key in [key1, key2]:
        if not key or len(key) < 64:
            return False

    # Remove 0x prefixes
    k1 = key1.lower()
    k2 = key2.lower()
    if k1.startswith("0x"):
        k1 = k1[2:]
    if k2.startswith("0x"):
        k2 = k2[2:]

    # Direct match
    if k1 == k2:
        return True

    # Normalize to body (without prefix)
    # k1_body: 128 chars (uncompressed) or 64 chars (compressed x-only)
    k1_body = k1
    k2_body = k2

    # Remove 04 prefix if present (uncompressed with prefix)
    if k1.startswith("04") and len(k1) == 130:
        k1_body = k1[2:]
    if k2.startswith("04") and len(k2) == 130:
        k2_body = k2[2:]

    # Check if direct match after removing 04 prefix
    if k1_body == k2_body:
        return True

    # Determine key types
    # Compressed: 66 chars (02/03 prefix + 64 chars x-coordinate)
    # Uncompressed: 128 chars (64 chars x + 64 chars y) or 130 chars (04 prefix + 128)

    k1_is_compressed = len(k1) == 66 and k1[:2] in ("02", "03")
    k2_is_compressed = len(k2) == 66 and k2[:2] in ("02", "03")
    k1_is_uncompressed = (len(k1) == 128) or (len(k1) == 130 and k1.startswith("04"))
    k2_is_uncompressed = (len(k2) == 128) or (len(k2) == 130 and k2.startswith("04"))

    try:
        if k1_is_compressed and k2_is_uncompressed:
            # k1 is compressed, k2 is uncompressed
            decompressed_k1 = decompress_public_key(k1)
            if decompressed_k1:
                # Remove 04 prefix from decompressed result if present
                if decompressed_k1.startswith("04"):
                    decompressed_k1 = decompressed_k1[2:]
                return decompressed_k1 == k2_body
        elif k1_is_uncompressed and k2_is_compressed:
            # k1 is uncompressed, k2 is compressed
            decompressed_k2 = decompress_public_key(k2)
            if decompressed_k2:
                # Remove 04 prefix from decompressed result if present
                if decompressed_k2.startswith("04"):
                    decompressed_k2 = decompressed_k2[2:]
                return k1_body == decompressed_k2
        elif k1_is_compressed and k2_is_compressed:
            # Both compressed - direct compare
            return k1 == k2
        elif k1_is_uncompressed and k2_is_uncompressed:
            # Both uncompressed - compare bodies
            return k1_body == k2_body
    except Exception:
        pass

    return False


@dataclass
class GmonadsConfig:
    """Configuration for gmonads API client"""

    base_url: str = DEFAULT_BASE_URL
    enabled: bool = True
    check_interval: int = 120  # 2 min cache
    timeout: int = 10


@dataclass
class EpochValidator:
    """Validator data from epoch endpoint"""
    node_id: str
    val_index: int
    stake: float
    commission: float
    validator_set_type: str  # "active" or other
    fetched_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "node_id": self.node_id,
            "val_index": self.val_index,
            "stake": self.stake,
            "commission": self.commission,
            "validator_set_type": self.validator_set_type,
            "fetched_at": self.fetched_at,
        }


@dataclass
class BlockMetrics:
    """Block metrics from 1m endpoint"""
    avg_tps: float
    avg_block_fullness_pct: float
    total_blocks: int
    total_txs: int
    fetched_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "avg_tps": self.avg_tps,
            "avg_block_fullness_pct": self.avg_block_fullness_pct,
            "total_blocks": self.total_blocks,
            "total_txs": self.total_txs,
            "fetched_at": self.fetched_at,
        }


@dataclass
class BlockMetricsTrend:
    """Block metrics trend from 1m-60m comparison endpoint"""
    current_tps: float
    previous_tps: float
    tps_change_percent: float
    current_fullness: float
    previous_fullness: float
    fullness_change_percent: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "current_tps": self.current_tps,
            "previous_tps": self.previous_tps,
            "tps_change_percent": self.tps_change_percent,
            "current_fullness": self.current_fullness,
            "previous_fullness": self.previous_fullness,
            "fullness_change_percent": self.fullness_change_percent,
        }


@dataclass
class NetworkHealth:
    """Network health summary"""
    tps: float
    tps_status: str  # "normal", "degraded", "critical"
    block_fullness: float
    active_validators: int
    alerts: List[str]
    fetched_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "tps": self.tps,
            "tps_status": self.tps_status,
            "block_fullness": self.block_fullness,
            "active_validators": self.active_validators,
            "alerts": self.alerts,
            "fetched_at": self.fetched_at,
        }


class GmonadsClient:
    """
    Client for gmonads.com public API.

    Provides network-wide metrics including TPS, block fullness,
    and validator set information.
    """

    def __init__(self, config: GmonadsConfig):
        self.config = config
        # Cache for epoch validators (per network)
        self._validators_cache: Dict[str, List[EpochValidator]] = {}
        self._validators_cache_times: Dict[str, float] = {}
        # Cache for block metrics (per network)
        self._metrics_cache: Dict[str, BlockMetrics] = {}
        self._metrics_cache_times: Dict[str, float] = {}
        # Cache for block metrics trend (per network)
        self._trend_cache: Dict[str, BlockMetricsTrend] = {}
        self._trend_cache_times: Dict[str, float] = {}
        # Cache for validator metadata (per network)
        self._metadata_cache: Dict[str, Dict] = {}
        self._metadata_cache_times: Dict[str, float] = {}

    def get_epoch_validators(self, network: str = "testnet") -> Optional[List[EpochValidator]]:
        """
        Get list of validators for current epoch.

        Args:
            network: Network name ('testnet' or 'mainnet')

        Returns:
            List of EpochValidator objects, or None on error
        """
        network_key = network.lower()
        now = time.time()

        # Check cache
        if network_key in self._validators_cache:
            cached_time = self._validators_cache_times.get(network_key, 0)
            if now - cached_time < self.config.check_interval:
                return self._validators_cache[network_key]

        url = f"{self.config.base_url}/validators/epoch"
        params = {"network": network_key}

        try:
            response = requests.get(url, params=params, timeout=self.config.timeout)
            response.raise_for_status()
            response_data = response.json()

            # API returns {"success": true, "data": [...]}
            items = response_data.get("data", [])

            validators = []
            for item in items:
                # Safe None handling for stake
                stake_val = item.get("stake")
                stake = float(stake_val) if stake_val is not None else 0.0

                # Safe None handling for commission
                commission_val = item.get("commission")
                commission = float(commission_val) if commission_val is not None else 0.0

                validators.append(EpochValidator(
                    node_id=item.get("node_id", ""),
                    val_index=item.get("val_index", 0),
                    stake=stake,
                    commission=commission,
                    validator_set_type=item.get("validator_set_type", "unknown"),
                    fetched_at=now,
                ))

            # Cache the result
            self._validators_cache[network_key] = validators
            self._validators_cache_times[network_key] = now

            return validators

        except requests.exceptions.RequestException as e:
            logger.warning(f"gmonads API error fetching validators for {network}: {e}")
            return self._validators_cache.get(network_key)
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"gmonads API parse error for {network}: {e}")
            return self._validators_cache.get(network_key)

    def get_block_metrics_1m(self, network: str = "testnet") -> Optional[BlockMetrics]:
        """
        Get block metrics for the last 1 minute.

        The API returns an array of per-minute buckets. We aggregate them
        to get overall metrics.

        Args:
            network: Network name ('testnet' or 'mainnet')

        Returns:
            BlockMetrics object, or None on error
        """
        network_key = network.lower()
        now = time.time()

        # Check cache (shorter cache time for metrics - 30 seconds)
        cache_ttl = min(30, self.config.check_interval)
        if network_key in self._metrics_cache:
            cached_time = self._metrics_cache_times.get(network_key, 0)
            if now - cached_time < cache_ttl:
                return self._metrics_cache[network_key]

        url = f"{self.config.base_url}/blocks/1m"
        params = {"network": network_key}

        try:
            response = requests.get(url, params=params, timeout=self.config.timeout)
            response.raise_for_status()
            response_data = response.json()

            # API returns {"success": true, "data": [...]} where data is array of buckets
            buckets = response_data.get("data", [])

            if not buckets:
                return self._metrics_cache.get(network_key)

            # Aggregate metrics from all buckets
            total_blocks = 0
            total_txs = 0
            total_tps = 0.0
            total_fullness = 0.0

            for bucket in buckets:
                # Safe None handling for all numeric conversions
                blocks_val = bucket.get("blocks")
                total_blocks += int(blocks_val) if blocks_val is not None else 0

                txs_val = bucket.get("txs")
                total_txs += int(txs_val) if txs_val is not None else 0

                tps_val = bucket.get("avg_tps")
                total_tps += float(tps_val) if tps_val is not None else 0.0

                fullness_val = bucket.get("avg_block_fullness_pct")
                total_fullness += float(fullness_val) if fullness_val is not None else 0.0

            # Calculate averages
            bucket_count = len(buckets)
            avg_tps = total_tps / bucket_count if bucket_count > 0 else 0.0
            avg_fullness = total_fullness / bucket_count if bucket_count > 0 else 0.0

            metrics = BlockMetrics(
                avg_tps=round(avg_tps, 2),
                avg_block_fullness_pct=round(avg_fullness, 2),
                total_blocks=total_blocks,
                total_txs=total_txs,
                fetched_at=now,
            )

            # Cache the result
            self._metrics_cache[network_key] = metrics
            self._metrics_cache_times[network_key] = now

            return metrics

        except requests.exceptions.RequestException as e:
            logger.warning(f"gmonads API error fetching block metrics for {network}: {e}")
            return self._metrics_cache.get(network_key)
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"gmonads API parse error for {network}: {e}")
            return self._metrics_cache.get(network_key)

    def get_block_metrics_trend(self, network: str = "testnet") -> Optional[BlockMetricsTrend]:
        """
        Get block metrics trend by comparing recent vs earlier buckets.

        Compares the most recent buckets with earlier ones to detect trends.

        Args:
            network: Network name ('testnet' or 'mainnet')

        Returns:
            BlockMetricsTrend object, or None on error
        """
        network_key = network.lower()
        now = time.time()

        # Check cache (1 minute cache for trend data)
        cache_ttl = min(60, self.config.check_interval)
        if network_key in self._trend_cache:
            cached_time = self._trend_cache_times.get(network_key, 0)
            if now - cached_time < cache_ttl:
                return self._trend_cache[network_key]

        url = f"{self.config.base_url}/blocks/1m"
        params = {"network": network_key}

        try:
            response = requests.get(url, params=params, timeout=self.config.timeout)
            response.raise_for_status()
            response_data = response.json()

            # API returns {"success": true, "data": [...]} where data is array of buckets
            buckets = response_data.get("data", [])

            if not buckets or len(buckets) < 2:
                return self._trend_cache.get(network_key)

            # Split buckets into recent (last 25%) and previous (first 75%)
            split_point = len(buckets) - (len(buckets) // 4)
            if split_point >= len(buckets):
                split_point = len(buckets) - 1

            recent_buckets = buckets[split_point:]  # Last buckets
            previous_buckets = buckets[:split_point]  # Earlier buckets

            # Helper function for safe float conversion
            def safe_float(value):
                return float(value) if value is not None else 0.0

            # Calculate averages for recent
            recent_tps = sum(safe_float(b.get("avg_tps")) for b in recent_buckets) / len(recent_buckets)
            recent_fullness = sum(safe_float(b.get("avg_block_fullness_pct")) for b in recent_buckets) / len(recent_buckets)

            # Calculate averages for previous
            previous_tps = sum(safe_float(b.get("avg_tps")) for b in previous_buckets) / len(previous_buckets)
            previous_fullness = sum(safe_float(b.get("avg_block_fullness_pct")) for b in previous_buckets) / len(previous_buckets)

            # Calculate change percentages
            tps_change = 0.0
            if previous_tps > 0:
                tps_change = ((recent_tps - previous_tps) / previous_tps) * 100

            fullness_change = 0.0
            if previous_fullness > 0:
                fullness_change = ((recent_fullness - previous_fullness) / previous_fullness) * 100

            trend = BlockMetricsTrend(
                current_tps=round(recent_tps, 2),
                previous_tps=round(previous_tps, 2),
                tps_change_percent=round(tps_change, 2),
                current_fullness=round(recent_fullness, 2),
                previous_fullness=round(previous_fullness, 2),
                fullness_change_percent=round(fullness_change, 2),
            )

            # Cache the result
            self._trend_cache[network_key] = trend
            self._trend_cache_times[network_key] = now

            return trend

        except requests.exceptions.RequestException as e:
            logger.warning(f"gmonads API error fetching block trend for {network}: {e}")
            return self._trend_cache.get(network_key)
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"gmonads API parse error for {network}: {e}")
            return self._trend_cache.get(network_key)

    def get_validator_metadata(self, network: str = "testnet") -> Optional[Dict]:
        """
        Get validator metadata including secp address mappings.

        Args:
            network: Network name ('testnet' or 'mainnet')

        Returns:
            Dictionary of validator metadata, or None on error
        """
        network_key = network.lower()
        now = time.time()

        # Check cache
        if network_key in self._metadata_cache:
            cached_time = self._metadata_cache_times.get(network_key, 0)
            if now - cached_time < self.config.check_interval:
                return self._metadata_cache[network_key]

        url = f"{self.config.base_url}/validators/metadata"
        params = {"network": network_key}

        try:
            response = requests.get(url, params=params, timeout=self.config.timeout)
            response.raise_for_status()
            data = response.json()

            # Cache the result
            self._metadata_cache[network_key] = data
            self._metadata_cache_times[network_key] = now

            return data

        except requests.exceptions.RequestException as e:
            logger.warning(f"gmonads API error fetching validator metadata for {network}: {e}")
            return self._metadata_cache.get(network_key)
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"gmonads API parse error for {network}: {e}")
            return self._metadata_cache.get(network_key)

    def is_validator_in_active_set(self, secp_address: str, network: str = "testnet") -> Optional[bool]:
        """
        Check if a validator is in the active set.

        Handles both compressed (66 hex chars, 02/03 prefix) and uncompressed
        (128/130 hex chars, with/without 04 prefix) public key formats.

        Args:
            secp_address: Validator's secp256k1 public key (compressed or uncompressed)
            network: Network name ('testnet' or 'mainnet')

        Returns:
            True if in active set, False if not, None if cannot determine
        """
        validators = self.get_epoch_validators(network)
        if validators is None:
            return None

        for v in validators:
            if public_keys_match(secp_address, v.node_id):
                return v.validator_set_type == "active"

        return None

    def get_active_validator_count(self, network: str = "testnet") -> int:
        """
        Get count of active validators.

        Args:
            network: Network name ('testnet' or 'mainnet')

        Returns:
            Number of active validators, or 0 on error
        """
        validators = self.get_epoch_validators(network)
        if validators is None:
            return 0

        return sum(1 for v in validators if v.validator_set_type == "active")

    def clear_cache(self) -> None:
        """Clear all cached data"""
        self._validators_cache.clear()
        self._validators_cache_times.clear()
        self._metrics_cache.clear()
        self._metrics_cache_times.clear()
        self._trend_cache.clear()
        self._trend_cache_times.clear()
        self._metadata_cache.clear()
        self._metadata_cache_times.clear()
