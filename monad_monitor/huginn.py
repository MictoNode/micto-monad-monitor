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

# Huginn Validator & Staking API v2 (OpenAPI 2.1.0)
# Canonical per-validator path is PLURAL /validators/uptime/{idOrAddress}.
# The legacy singular path (/validator/uptime/...) still responds but is
# undocumented and may disappear; do not rely on it.
# Without query params the API returns a rolling 24h raw-event window; pass
# period=all to get cumulative totals from the permanent epoch snapshots
# (the semantics this monitor's counters were designed around).
VALIDATOR_UPTIME_PATH = "/validators/uptime/"
UPTIME_PERIOD_QUERY = "?period=all"
VALIDATOR_PATH = "/validators/"
VALIDATOR_HEALTH_SUFFIX = "/health"
STATUS_PATH = "/status"

# Validator API "status" field values (v2)
STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
STATUS_PENDING = "pending"

# Data-freshness gating (/status). Huginn serves uptime verdicts from SQLite
# even when its consensus node is unreachable, so a stale "inactive" verdict
# must not be trusted (fall back to gmonads instead). Raw round events arrive
# every block (~0.3s), so silence longer than the threshold means the source
# has lost its node.
MAX_HUGINN_DATA_AGE_SECONDS = 300  # raw events silent this long -> data stale
FRESHNESS_CHECK_INTERVAL = 60      # per-network /status poll interval (seconds)


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
    check_interval: int = 3600  # 1 hour cache (validator endpoints are not rate limited per docs; cache keeps verdicts stable and reduces load)
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
    # v2 /health merge (best-effort; None when health endpoint unavailable)
    uptime_24h: Optional[float] = None  # rolling 24h uptime %
    health_state: Optional[str] = None  # "healthy"/"stale"/"no_data"/... from Huginn /health
    seconds_since_last_event: Optional[int] = None  # network-side liveness (seconds)
    last_event_utc: Optional[str] = None  # newest observed round event (UTC)

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
            "uptime_24h": self.uptime_24h,
            "health_state": self.health_state,
            "seconds_since_last_event": self.seconds_since_last_event,
            "last_event_utc": self.last_event_utc,
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
        # /status freshness cache: key "network" -> {"seconds_since_newest": int|None, "checked_at": float}
        self._status_cache: Dict[str, Dict[str, Any]] = {}
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
        Get validator uptime data from Huginn API (API v2).

        Calls the canonical /validators/uptime/{secp} endpoint with
        period=all so counters are CUMULATIVE epoch-snapshot totals, not the
        rolling 24h raw-event window the API returns by default. Cumulative
        totals are what keep is_ever_active and the timeout-count increase
        baseline meaningful (see main.timeout_increase_to_report).

        Cache is per (network, secp_address) tuple. Verdicts are additionally
        gated on /status freshness: if Huginn's raw round events have been
        silent too long (its node is unreachable, SQLite keeps serving stale
        data), an inactive/pending verdict is downgraded to None so metrics.py
        falls back to gmonads instead of raising a false "LEFT ACTIVE SET".

        Best-effort /validators/{secp}/health merge restores last_round /
        last_block_height (removed from the v2 uptime payload) and adds
        uptime_24h / health_state / seconds_since_last_event.

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

        # Fetch cumulative uptime totals from the canonical v2 endpoint
        url = f"{base_url}{VALIDATOR_UPTIME_PATH}{secp_address}{UPTIME_PERIOD_QUERY}"
        response_data = self._get_json(url, network, secp_address)

        if response_data is None:
            # Error, rate limited, or not found - return cached data if any
            return self._cache.get(cache_key)

        # Extract uptime data from response (API returns {"success": true, "uptime": {...}})
        data = response_data.get("uptime", response_data) if isinstance(response_data, dict) else response_data

        # Parse response — active set detection from API status field only
        uptime = self._parse_uptime_response(secp_address, data)

        if uptime:
            # Merge /health liveness fields (best-effort, never fails the fetch)
            self._enrich_with_health(uptime, base_url, network)
            # Downgrade inactive/pending verdicts when Huginn data is stale
            self._apply_freshness_gate(uptime, network)

        # Cache the result
        if uptime:
            self._cache[cache_key] = uptime
            self._cache_times[cache_key] = now

        return uptime

    def _get_json(
        self, url: str, network: str, subject: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch a URL and decode JSON, honoring retry/circuit breaker/rate limit.

        Returns None on any failure (server error, rate limit, HTTP >= 400,
        network error, invalid JSON). Callers fall back to their cache.
        """
        response = self._fetch_with_retry(url, network, self.config.timeout)

        if response is None:
            return None

        # Handle rate limiting
        if response.status_code == 429:
            who = f" for {subject[:16]}..." if subject else ""
            self._logger.warning(f"Huginn API rate limited{who} on {network}")
            return None

        if response.status_code >= 400:
            who = f" for {subject[:16]}..." if subject else ""
            self._logger.warning(
                f"Huginn API error (HTTP {response.status_code}){who} on {network}"
            )
            return None

        try:
            return response.json()
        except ValueError as e:
            self._logger.error(f"Huginn API parse error on {network}: {e}")
            return None

    def _get_json_aux(
        self, url: str, network: str, subject: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Single-shot JSON fetch for AUXILIARY endpoints (/health, /status).

        These are best-effort enrichment/freshness reads polled on every cache
        refresh; they must not retry (would stall the monitor loop during a
        Huginn outage) and must not touch the shared circuit breaker (their
        failures would open it and block the PRIMARY uptime fetch too).
        Any failure is logged at debug and returns None (fail-open).
        """
        try:
            response = requests.get(url, timeout=self.config.timeout)
        except RequestException as e:
            who = f" for {subject[:16]}..." if subject else ""
            self._logger.debug(f"Huginn aux request failed{who} on {network}: {e}")
            return None

        if response.status_code >= 400:
            who = f" for {subject[:16]}..." if subject else ""
            self._logger.debug(
                f"Huginn aux endpoint HTTP {response.status_code}{who} on {network}"
            )
            return None

        try:
            return response.json()
        except ValueError as e:
            self._logger.debug(f"Huginn aux parse error on {network}: {e}")
            return None

    def _enrich_with_health(
        self, uptime: ValidatorUptime, base_url: str, network: str
    ) -> None:
        """
        Best-effort merge of /validators/{secp}/health into ValidatorUptime.

        The v2 uptime payload no longer carries last_round/last_block_height;
        /health does, plus a purpose-built liveness view (uptime_24h,
        health_state, seconds_since_last_event). Any health failure only logs
        at debug level - the uptime result stands.
        """
        url = f"{base_url}{VALIDATOR_PATH}{uptime.secp_address}{VALIDATOR_HEALTH_SUFFIX}"
        health_data = self._get_json_aux(url, network, uptime.secp_address)
        if health_data is None:
            return
        health = health_data.get("health") if isinstance(health_data, dict) else None
        if not isinstance(health, dict):
            return

        uptime.last_round = health.get("last_round", uptime.last_round)
        uptime.last_block_height = health.get("last_block_height", uptime.last_block_height)
        uptime.uptime_24h = health.get("uptime_24h")
        uptime.health_state = health.get("state")
        uptime.seconds_since_last_event = health.get("seconds_since_last_event")
        uptime.last_event_utc = health.get("last_event_utc")

    def _is_huginn_data_stale(self, network: str) -> bool:
        """
        Whether Huginn's raw round events have been silent too long (per /status).

        Huginn serves uptime data from SQLite even when its consensus node is
        unreachable; raw round events arrive every block, so silence past
        MAX_HUGINN_DATA_AGE_SECONDS means every verdict served is stale.

        /status itself is polled at most once per FRESHNESS_CHECK_INTERVAL per
        network. Fail-open: if /status is unavailable, data is treated as fresh.
        """
        cache_key = f"freshness:{network.lower()}"
        now = time.time()
        cached = self._status_cache.get(cache_key)
        if cached and now - cached.get("checked_at", 0) < FRESHNESS_CHECK_INTERVAL:
            seconds_since_newest = cached.get("seconds_since_newest")
        else:
            base_url = self.config.get_endpoint(network)
            status_data = self._get_json_aux(f"{base_url}{STATUS_PATH}", network)
            seconds_since_newest = None
            if isinstance(status_data, dict):
                uptime_events = status_data.get("uptime_events") or {}
                seconds_since_newest = uptime_events.get("seconds_since_newest")
            self._status_cache[cache_key] = {
                "checked_at": now,
                "seconds_since_newest": seconds_since_newest,
            }

        if seconds_since_newest is None:
            return False  # fail-open: unknown freshness

        if seconds_since_newest > MAX_HUGINN_DATA_AGE_SECONDS:
            self._logger.warning(
                f"Huginn data is STALE for {network} "
                f"({seconds_since_newest}s since last raw round event)"
            )
            return True
        return False

    def _apply_freshness_gate(self, uptime: ValidatorUptime, network: str) -> None:
        """
        Downgrade inactive/pending verdicts to None (unknown) while Huginn's
        data is stale, so callers fall back to gmonads instead of trusting a
        frozen "inactive" and raising a false "LEFT ACTIVE SET" alert.

        Active verdicts are left untouched: downgrading them could mask a real
        exit; the next fresh fetch corrects any lag.
        """
        if uptime.is_active is not False:
            return
        if not self._is_huginn_data_stale(network):
            return
        self._logger.warning(
            f"Huginn verdict for {uptime.secp_address[:16]}... on {network} is "
            f"inactive/pending but data is stale - downgrading to unknown "
            f"(gmonads fallback)"
        )
        uptime.is_active = None

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

        # Determine active set status from API's "status" field (v2 values:
        # "active" | "inactive" | "pending"). Only "active" means in-set;
        # inactive and pending both report False. Any present-but-unexpected
        # value (e.g. a future "unknown") is treated as unknown -> gmonads
        # fallback rather than a confident False.
        api_status = data.get("status")
        if api_status == STATUS_ACTIVE:
            is_active = True
        elif api_status in (STATUS_INACTIVE, STATUS_PENDING):
            is_active = False
        elif api_status is not None:
            is_active = None  # Unknown value -> metrics.py gmonads fallback
            self._logger.warning(
                f"Huginn unexpected status {api_status!r} for "
                f"{secp_address[:16]}..., falling back to gmonads"
            )
        else:
            is_active = None  # Missing field -> metrics.py gmonads fallback
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
