"""Huginn Tech Validator API client for external uptime verification with multi-network support

Bu modul Huginn Tech API'si ile iletisim kurarak validator uptime bilgilerini saglar.
Multi-validator stratejisi ile ag round referansi alir ve circuit breaker ile dayaniklilik saglar.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from enum import Enum, auto

import requests
from requests.exceptions import RequestException


# Default API endpoints for each network
DEFAULT_ENDPOINTS = {
    "testnet": "https://validator-api-testnet.huginn.tech/monad-api",
    "mainnet": "https://validator-api.huginn.tech/monad-api",
}

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds
RETRY_MAX_DELAY = 5.0  # seconds

# Circuit breaker configuration
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
CIRCUIT_BREAKER_RECOVERY_TIME = 60  # seconds


class CircuitState(Enum):
    """Circuit breaker durumları"""
    CLOSED = auto()      # Normal operation
    OPEN = auto()        # Failing fast, no requests
    HALF_OPEN = auto()   # Testing if recovered


@dataclass
class HuginnConfig:
    """Configuration for Huginn API client with multi-network support"""

    endpoints: Dict[str, str] = field(default_factory=lambda: DEFAULT_ENDPOINTS.copy())
    enabled: bool = True
    check_interval: int = 3600  # 1 hour cache (rate limit: 5 validators/hour)
    timeout: int = 10
    # Legacy support: base_url overrides endpoints if provided
    base_url: Optional[str] = None

    def get_endpoint(self, network: Optional[str] = None) -> str:
        """
        Get API endpoint for the specified network.

        Args:
            network: Network name ('testnet', 'mainnet'). Defaults to 'testnet'.

        Returns:
            API endpoint URL for the network
        """
        # Legacy support: if base_url is set, use it for all networks
        if self.base_url:
            return self.base_url

        # Default to testnet for unknown/missing network
        network_key = (network or "testnet").lower()

        # Return the endpoint if it exists, otherwise default to testnet
        return self.endpoints.get(network_key, self.endpoints.get("testnet", DEFAULT_ENDPOINTS["testnet"]))


@dataclass
class ValidatorUptime:
    """Validator uptime data from Huginn API"""
    validator_id: Optional[int]
    validator_name: Optional[str]
    secp_address: str
    is_active: Optional[bool]  # True/False from API status field, None if unknown → gmonads fallback
    is_ever_active: bool  # Has ever been in active set (total_events > 0)
    uptime_percent: float
    finalized_count: int
    timeout_count: int
    total_events: int
    last_round: Optional[int]
    last_block_height: Optional[int]
    since_utc: Optional[str]
    fetched_at: float  # Unix timestamp

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "validator_id": self.validator_id,
            "validator_name": self.validator_name,
            "secp_address": self.secp_address,
            "is_active": self.is_active,
            "is_ever_active": self.is_ever_active,
            "uptime_percent": self.uptime_percent,
            "finalized_count": self.finalized_count,
            "timeout_count": self.timeout_count,
            "total_events": self.total_events,
            "last_round": self.last_round,
            "last_block_height": self.last_block_height,
            "since_utc": self.since_utc,
            "fetched_at": self.fetched_at,
        }


class CircuitBreaker:
    """
    Circuit breaker for API resilience.

    5 basarisizliktan sonra 60 saniye bekler ve tekrar dener.
    """

    def __init__(
        self,
        failure_threshold: int = CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        recovery_time: int = CIRCUIT_BREAKER_RECOVERY_TIME,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.last_failure_time: Optional[float] = None
        self._logger = logging.getLogger(__name__)

    def can_execute(self) -> bool:
        """Check if request can be executed"""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if recovery time has passed
            if self.last_failure_time and (time.time() - self.last_failure_time >= self.recovery_time):
                self.state = CircuitState.HALF_OPEN
                self._logger.info("Circuit breaker: OPEN -> HALF_OPEN, testing recovery")
                return True
            return False

        # HALF_OPEN - allow one request to test
        return True

    def record_success(self) -> None:
        """Record successful request"""
        if self.state == CircuitState.HALF_OPEN:
            self._logger.info("Circuit breaker: HALF_OPEN -> CLOSED, recovered")
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Record failed request"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            if self.state != CircuitState.OPEN:
                self._logger.warning(
                    f"Circuit breaker: {self.state.name} -> OPEN after {self.failure_count} failures"
                )
            self.state = CircuitState.OPEN

    def is_open(self) -> bool:
        """Check if circuit is open (failing fast)"""
        return self.state == CircuitState.OPEN


class HuginnClient:
    """
    Client for Huginn Tech Validator API with multi-network support.

    Provides external verification of validator active set status,
    which is more reliable than inference from local metrics.

    Supports both testnet and mainnet networks with separate caching.

    Active set detection logic:
    - Uses Huginn API's "status" field directly ("active"/"inactive")
    - If status field is missing, is_active is set to None → triggers gmonads fallback
    - A validator is "ever_active" if it has total_events > 0 (has participated before)

    Resilience features:
    - Retry with exponential backoff (3 retries for 5xx errors)
    - Circuit breaker (5 failures -> 60s pause)
    - Proper logging instead of print()
    """

    def __init__(self, config: HuginnConfig):
        self.config = config
        # Cache key format: "network:secp_address" for per-network caching
        self._cache: Dict[str, ValidatorUptime] = {}
        self._cache_times: Dict[str, float] = {}
        # Circuit breaker for each network
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}
        # Logger
        self._logger = logging.getLogger(__name__)

    def _get_circuit_breaker(self, network: str) -> CircuitBreaker:
        """Get or create circuit breaker for network"""
        network_key = network.lower()
        if network_key not in self._circuit_breakers:
            self._circuit_breakers[network_key] = CircuitBreaker()
        return self._circuit_breakers[network_key]

    def _fetch_with_retry(
        self,
        url: str,
        network: str,
        timeout: int
    ) -> Optional[requests.Response]:
        """
        Fetch URL with retry and circuit breaker.

        Only retries on 5xx server errors, not on 4xx client errors.
        Uses exponential backoff between retries.
        """
        circuit_breaker = self._get_circuit_breaker(network)

        # Check circuit breaker
        if not circuit_breaker.can_execute():
            self._logger.warning(f"Circuit breaker OPEN for {network}, skipping request")
            return None

        last_exception = None

        for attempt in range(MAX_RETRIES):
            try:
                response = requests.get(url, timeout=timeout)

                # Success
                if response.status_code < 500:
                    circuit_breaker.record_success()
                    return response

                # 5xx error - retry
                if response.status_code >= 500:
                    last_exception = Exception(f"HTTP {response.status_code}")
                    self._logger.warning(
                        f"Huginn API server error (HTTP {response.status_code}) "
                        f"for {network}, attempt {attempt + 1}/{MAX_RETRIES}"
                    )

                    if attempt < MAX_RETRIES - 1:
                        # Exponential backoff
                        delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                        time.sleep(delay)
                        continue

                    # Last attempt failed
                    circuit_breaker.record_failure()
                    return response

            except RequestException as e:
                last_exception = e
                self._logger.warning(
                    f"Huginn API request failed for {network}: {e}, "
                    f"attempt {attempt + 1}/{MAX_RETRIES}"
                )

                if attempt < MAX_RETRIES - 1:
                    delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                    time.sleep(delay)
                    continue

        # All retries failed
        circuit_breaker.record_failure()
        self._logger.error(
            f"All {MAX_RETRIES} retries failed for {network}: {last_exception}"
        )
        return None

    def get_validator_uptime(
        self, secp_address: Optional[str], network: str = "testnet",
        gmonads_client: Optional[Any] = None
    ) -> Optional[ValidatorUptime]:
        """
        Get validator uptime data from Huginn API.

        Uses caching to respect rate limits (5 validators/hour).
        Cache is per (network, secp_address) tuple.

        Active set status is determined from Huginn API's "status" field.
        If the status field is missing, is_active is set to None, which
        triggers gmonads fallback in metrics.py.

        Args:
            secp_address: The validator's secp256k1 public key
            network: Network name ('testnet' or 'mainnet'). Defaults to 'testnet'.
            gmonads_client: Unused, kept for API compatibility.

        Returns:
            ValidatorUptime if successful, None on error or rate limited
        """
        if not secp_address:
            return None

        # Check cache validity - use network-prefixed cache key
        now = time.time()
        cache_key = f"{network.lower()}:{secp_address.lower()}"

        if cache_key in self._cache:
            cached_time = self._cache_times.get(cache_key, 0)
            if now - cached_time < self.config.check_interval:
                return self._cache[cache_key]

        # Get endpoint for the specified network
        base_url = self.config.get_endpoint(network)

        # Fetch from API with retry and circuit breaker
        url = f"{base_url}/validator/uptime/{secp_address}"
        response = self._fetch_with_retry(url, network, self.config.timeout)

        if response is None:
            # Return cached data if available, even if stale
            return self._cache.get(cache_key)

        # Handle rate limiting
        if response.status_code == 429:
            self._logger.warning(
                f"Huginn API rate limited for {secp_address[:16]}... on {network}"
            )
            return self._cache.get(cache_key)

        if response.status_code >= 400:
            self._logger.warning(
                f"Huginn API error (HTTP {response.status_code}) for "
                f"{secp_address[:16]}... on {network}"
            )
            return self._cache.get(cache_key)

        try:
            response_data = response.json()
        except ValueError as e:
            self._logger.error(f"Huginn API parse error on {network}: {e}")
            return self._cache.get(cache_key)

        # Extract uptime data from response (API returns {"success": true, "uptime": {...}})
        data = response_data.get("uptime", response_data) if isinstance(response_data, dict) else response_data

        # Parse response — active set detection from API status field only
        uptime = self._parse_uptime_response(secp_address, data)

        # Cache the result
        if uptime:
            self._cache[cache_key] = uptime
            self._cache_times[cache_key] = now

        return uptime

    def _parse_uptime_response(
        self, secp_address: str, data: Dict[str, Any]
    ) -> Optional[ValidatorUptime]:
        """
        Parse API response into ValidatorUptime.

        Active set detection uses ONLY the API's "status" field.
        If status is missing, is_active is set to None, which triggers
        gmonads fallback in metrics.py.

        Args:
            secp_address: Validator's secp address
            data: API response data

        Returns:
            ValidatorUptime object or None if data is invalid
        """
        if not data:
            return None

        # Extract uptime data
        total_events = data.get("total_events", 0) or 0
        is_ever_active = total_events > 0

        # Calculate uptime percentage
        finalized = data.get("finalized_count", 0) or 0
        timeouts = data.get("timeout_count", 0) or 0

        if total_events > 0:
            uptime_percent = (finalized / total_events) * 100
        else:
            uptime_percent = 0.0

        # Determine active set status from API's "status" field only
        api_status = data.get("status")
        if api_status is not None:
            is_active = api_status == "active"
        else:
            is_active = None  # Unknown → metrics.py gmonads fallback
            self._logger.debug(
                f"Huginn status field missing for {secp_address[:16]}..., "
                f"falling back to gmonads"
            )

        return ValidatorUptime(
            validator_id=data.get("validator_id"),
            validator_name=data.get("validator_name"),
            secp_address=secp_address,
            is_active=is_active,
            is_ever_active=is_ever_active,
            uptime_percent=round(uptime_percent, 2),
            finalized_count=finalized,
            timeout_count=timeouts,
            total_events=total_events,
            last_round=data.get("last_round"),
            last_block_height=data.get("last_block_height"),
            since_utc=data.get("since_utc"),
            fetched_at=time.time(),
        )

    def is_validator_active(
        self, secp_address: Optional[str], network: str = "testnet"
    ) -> Optional[bool]:
        """
        Quick check if validator is currently in active set.

        Active set status is determined from Huginn API's "status" field.

        Args:
            secp_address: The validator's secp256k1 public key
            network: Network name ('testnet' or 'mainnet'). Defaults to 'testnet'.

        Returns:
            True if status="active", False if status="inactive",
            None if cannot determine (API error, missing address, no status field, etc.)
        """
        uptime = self.get_validator_uptime(secp_address, network=network)
        return uptime.is_active if uptime else None

    def get_circuit_breaker_status(self, network: str = "testnet") -> Dict[str, Any]:
        """
        Get circuit breaker status for monitoring.

        Args:
            network: Network name

        Returns:
            Dict with circuit breaker state info
        """
        cb = self._get_circuit_breaker(network)
        return {
            "state": cb.state.name,
            "failure_count": cb.failure_count,
            "is_open": cb.is_open(),
        }

    def clear_cache(self) -> None:
        """Clear all cached data"""
        self._cache.clear()
        self._cache_times.clear()

    def get_cache_age(
        self, secp_address: str, network: str = "testnet"
    ) -> Optional[float]:
        """
        Get age of cached data in seconds.

        Args:
            secp_address: The validator's secp256k1 public key
            network: Network name ('testnet' or 'mainnet'). Defaults to 'testnet'.

        Returns:
            Age in seconds if cached, None if not cached
        """
        cache_key = f"{network.lower()}:{secp_address.lower()}"
        if cache_key not in self._cache_times:
            return None
        return time.time() - self._cache_times[cache_key]
