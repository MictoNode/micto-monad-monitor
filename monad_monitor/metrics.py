"""Prometheus metrics scraper - remote version"""

import logging
import re
from typing import Any, Dict, Optional, TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from monad_monitor.huginn import HuginnClient

logger = logging.getLogger(__name__)


class MetricsScraper:
    """Scrape Prometheus metrics from remote validator nodes"""

    def __init__(
        self, metrics_url: str, rpc_url: str, timeout: int = 10
    ):
        self.metrics_url = metrics_url
        self.rpc_url = rpc_url
        self.timeout = timeout

    def fetch_metrics(self) -> Optional[str]:
        """Fetch metrics from remote Prometheus endpoint"""
        try:
            response = requests.get(self.metrics_url, timeout=self.timeout)
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException as e:
            logger.debug(f"Metrics fetch error: {e}")
            return None

    def parse_metric(self, metrics_text: str, metric_name: str) -> Optional[float]:
        """Parse a single metric value from Prometheus text format"""
        # Pattern matches: metric_name{...} value or metric_name value
        # Supports: integers, decimals, scientific notation (e.g., 1.4896736e+07), NaN, -Inf, +Inf
        # Numeric pattern: optional sign, digits with optional decimal, optional exponent part
        numeric_pattern = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
        pattern = rf"^{metric_name}(?:\{{[^}}]*\}})?\s+({numeric_pattern}|NaN|-Inf|\+Inf)"
        match = re.search(pattern, metrics_text, re.MULTILINE)

        if match:
            value = match.group(1)
            # Handle special Prometheus values
            if value in ("NaN", "-Inf", "+Inf"):
                return None
            return float(value)
        return None

    def get_monad_metrics(self) -> Dict:
        """Fetch Monad-specific metrics from validator"""
        raw = self.fetch_metrics()

        if not raw:
            return {"error": "Could not fetch metrics"}

        return {
            # Core consensus metrics
            "block_commits": self.parse_metric(
                raw, "monad_execution_ledger_num_commits"
            ),
            "block_height": self.parse_metric(
                raw, "monad_execution_ledger_block_num"
            ),
            "local_timeout": self.parse_metric(
                raw, "monad_state_consensus_events_local_timeout"
            ),
            "execution_lagging": self.parse_metric(
                raw, "monad_state_consensus_events_rx_execution_lagging"
            ),
            "ts_validation_fail": self.parse_metric(
                raw, "monad_state_consensus_events_failed_ts_validation"
            ),
            "blocksync": self.parse_metric(
                raw, "monad_state_blocksync_events_payload_response_successful"
            ),
            "proposals": self.parse_metric(
                raw, "monad_bft_txpool_create_proposal"
            ),
            "peers": self.parse_metric(raw, "monad_peer_disc_num_peers"),
            "syncing": self.parse_metric(raw, "monad_statesync_syncing"),
        }

    def get_system_metrics(self, node_exporter_url: str) -> Dict:
        """Fetch system metrics from Node Exporter (optional)"""
        if not node_exporter_url:
            return {}

        try:
            resp = requests.get(node_exporter_url, timeout=self.timeout)
            resp.raise_for_status()
            raw = resp.text

            # Parse CPU metrics - calculate usage from idle
            cpu_idle = self._parse_cpu_idle(raw)
            mem_total = self.parse_metric(raw, "node_memory_MemTotal_bytes")
            mem_available = self.parse_metric(raw, "node_memory_MemAvailable_bytes")
            mem_used = None
            mem_percent = None
            if mem_total and mem_available:
                mem_used = mem_total - mem_available
                mem_percent = (mem_used / mem_total) * 100

            # Parse disk metrics
            disk_metrics = self._parse_disk_metrics(raw)

            # Parse TrieDB metrics
            triedb_metrics = self._parse_triedb_metrics(raw)

            return {
                # CPU
                "cpu_idle_percent": cpu_idle,
                "cpu_used_percent": 100 - cpu_idle if cpu_idle else None,
                # Memory
                "mem_total": mem_total,
                "mem_available": mem_available,
                "mem_used": mem_used,
                "mem_percent": mem_percent,
                # Disk
                "disk_total_bytes": disk_metrics.get("total"),
                "disk_used_bytes": disk_metrics.get("used"),
                "disk_avail_bytes": disk_metrics.get("available"),
                "disk_percent": disk_metrics.get("percent"),
                # TrieDB
                "triedb": triedb_metrics,
            }
        except requests.exceptions.RequestException:
            return {}

    def _parse_cpu_idle(self, raw: str) -> Optional[float]:
        """Parse CPU idle percentage from node exporter metrics"""
        # Get all CPU modes and calculate idle percentage
        idle_pattern = r'^node_cpu_seconds_total\{.*mode="idle".*\}\s+([\d.]+)'
        total_pattern = r'^node_cpu_seconds_total\{.*\}\s+([\d.]+)'

        idle_matches = re.findall(idle_pattern, raw, re.MULTILINE)
        total_matches = re.findall(total_pattern, raw, re.MULTILINE)

        if idle_matches and total_matches:
            total_idle = sum(float(m) for m in idle_matches)
            total_all = sum(float(m) for m in total_matches)
            if total_all > 0:
                return (total_idle / total_all) * 100
        return None

    def _parse_disk_metrics(self, raw: str) -> Dict:
        """Parse disk usage metrics from node exporter"""
        result = {}

        # Look for root filesystem metrics
        avail_pattern = r'^node_filesystem_avail_bytes\{.*mount="/".*\}\s+([\d.]+)'
        size_pattern = r'^node_filesystem_size_bytes\{.*mount="/".*\}\s+([\d.]+)'

        avail_match = re.search(avail_pattern, raw, re.MULTILINE)
        size_match = re.search(size_pattern, raw, re.MULTILINE)

        if size_match:
            result["total"] = float(size_match.group(1))
        if avail_match:
            result["available"] = float(avail_match.group(1))

        if result.get("total") and result.get("available"):
            result["used"] = result["total"] - result["available"]
            result["percent"] = (result["used"] / result["total"]) * 100

        return result

    def _parse_triedb_metrics(self, raw: str) -> Dict:
        """Parse TrieDB metrics from node exporter textfile collector"""
        result = {}

        # Main TrieDB metrics (with drive="triedb" label)
        used_pattern = r'^monad_triedb_used_bytes\{drive="triedb"\}\s+([\d.e+-]+)'
        capacity_pattern = r'^monad_triedb_capacity_bytes\{drive="triedb"\}\s+([\d.e+-]+)'
        avail_pattern = r'^monad_triedb_avail_bytes\{drive="triedb"\}\s+([\d.e+-]+)'
        percent_pattern = r'^monad_triedb_used_percent\{drive="triedb"\}\s+([\d.e+-]+)'

        used_match = re.search(used_pattern, raw, re.MULTILINE)
        capacity_match = re.search(capacity_pattern, raw, re.MULTILINE)
        avail_match = re.search(avail_pattern, raw, re.MULTILINE)
        percent_match = re.search(percent_pattern, raw, re.MULTILINE)

        if used_match:
            result["used_bytes"] = float(used_match.group(1))
        if capacity_match:
            result["capacity_bytes"] = float(capacity_match.group(1))
        if avail_match:
            result["avail_bytes"] = float(avail_match.group(1))
        if percent_match:
            result["used_percent"] = float(percent_match.group(1))

        # Fast chunks metrics
        fast_chunks = self.parse_metric(raw, "monad_triedb_fast_chunks")
        fast_used = self.parse_metric(raw, "monad_triedb_fast_used_bytes")
        fast_capacity = self.parse_metric(raw, "monad_triedb_fast_capacity_bytes")

        if fast_chunks is not None:
            result["fast_chunks"] = int(fast_chunks)
        if fast_used is not None:
            result["fast_used_bytes"] = fast_used
        if fast_capacity is not None:
            result["fast_capacity_bytes"] = fast_capacity

        # Slow chunks metrics
        slow_chunks = self.parse_metric(raw, "monad_triedb_slow_chunks")
        slow_used = self.parse_metric(raw, "monad_triedb_slow_used_bytes")
        slow_capacity = self.parse_metric(raw, "monad_triedb_slow_capacity_bytes")

        if slow_chunks is not None:
            result["slow_chunks"] = int(slow_chunks)
        if slow_used is not None:
            result["slow_used_bytes"] = slow_used
        if slow_capacity is not None:
            result["slow_capacity_bytes"] = slow_capacity

        # Free chunks
        free_chunks = self.parse_metric(raw, "monad_triedb_free_chunks")
        if free_chunks is not None:
            result["free_chunks"] = int(free_chunks)

        # History metrics
        history_count = self.parse_metric(raw, "monad_triedb_history_count")
        history_max = self.parse_metric(raw, "monad_triedb_history_max")

        if history_count is not None:
            result["history_count"] = int(history_count)
        if history_max is not None:
            result["history_max"] = int(history_max)

        return result

    def check_rpc_health(self) -> bool:
        """Check if RPC endpoint is responding"""
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_blockNumber",
                "params": [],
                "id": 1,
            }
            response = requests.post(
                self.rpc_url,
                json=payload,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            result = response.json()
            return "result" in result
        except requests.exceptions.RequestException:
            return False

    def get_validator_status(
        self,
        validator_secp: str,
        huginn_client: Optional["HuginnClient"] = None,
        network: str = "testnet",
        gmonads_client: Optional[Any] = None,
    ) -> Dict:
        """
        Determine if validator is in the active set.

        Priority order:
        1. Huginn API (most detailed - includes uptime, timeout_count, etc.)
        2. gmonads API (reliable fallback - active set verification)
        3. Local Prometheus metrics inference (last resort)

        Args:
            validator_secp: The validator's secp256k1 public key for identification
            huginn_client: Optional Huginn API client for external verification
            network: Network name ('testnet' or 'mainnet'). Defaults to 'testnet'.
            gmonads_client: Optional GmonadsClient for active set verification fallback.

        Returns:
            Dict with 'is_active' (bool), 'reason' (str), 'source' (str),
            and additional data depending on source
        """
        # Try Huginn API first if client is provided (most detailed)
        if huginn_client and validator_secp:
            uptime = huginn_client.get_validator_uptime(
                validator_secp, network=network, gmonads_client=gmonads_client
            )
            if uptime:
                return {
                    "is_active": uptime.is_active,
                    "reason": (
                        f"Verified via Huginn API: {uptime.total_events} events, "
                        f"{uptime.uptime_percent}% uptime"
                    ),
                    "source": "huginn_api",
                    "uptime_percent": uptime.uptime_percent,
                    "finalized_count": uptime.finalized_count,
                    "timeout_count": uptime.timeout_count,
                    "total_events": uptime.total_events,
                    "last_round": uptime.last_round,
                    "last_block_height": uptime.last_block_height,
                    "huginn_data": uptime.to_dict(),
                }

        # Fallback 1: Try gmonads API for active set verification
        # This is more reliable than local inference when Huginn is unavailable
        if gmonads_client and validator_secp:
            is_active = gmonads_client.is_validator_in_active_set(validator_secp, network)
            if is_active is not None:
                logger.info(
                    f"Validator status determined via gmonads (Huginn unavailable): "
                    f"is_active={is_active}"
                )
                return {
                    "is_active": is_active,
                    "reason": f"Verified via gmonads API (Huginn unavailable)",
                    "source": "gmonads_api",
                }

        # Fallback 2: Infer from local Prometheus metrics (last resort)
        return self._infer_validator_status(validator_secp)

    def _infer_validator_status(self, validator_secp: str) -> Dict:
        """
        Infer validator active status from local Prometheus metrics.

        Used as fallback when Huginn API and gmonads API are both unavailable.

        IMPORTANT: local_timeout metric is NOT used here because it tracks
        OTHER validators' timeouts, not our validator's status. Using it
        would cause false positives during network-wide issues.

        Args:
            validator_secp: The validator's secp256k1 public key (for logging)

        Returns:
            Dict with 'is_active', 'reason', 'source', and 'metrics_used'
        """
        raw = self.fetch_metrics()

        if not raw:
            return {
                "is_active": None,  # Unknown - cannot determine
                "reason": "Could not fetch metrics to determine validator status",
                "source": "inference",
                "metrics_used": [],
            }

        metrics_used = []

        # Strategy 1: Check for proposal creation (indicates active proposer)
        proposals = self.parse_metric(raw, "monad_bft_txpool_create_proposal")
        if proposals is not None and proposals > 0:
            metrics_used.append("monad_bft_txpool_create_proposal")
            return {
                "is_active": True,
                "reason": f"Validator has created {int(proposals)} proposals",
                "source": "inference",
                "metrics_used": metrics_used,
                "proposals_count": int(proposals),
            }

        # Strategy 2: Check block commits
        # If validator has commits, it's participating in consensus
        block_commits = self.parse_metric(raw, "monad_execution_ledger_num_commits")
        if block_commits is not None and block_commits > 0:
            metrics_used.append("monad_execution_ledger_num_commits")
            return {
                "is_active": True,
                "reason": f"Validator has {int(block_commits)} block commits",
                "source": "inference",
                "metrics_used": metrics_used,
                "block_commits": int(block_commits),
            }

        # Fallback: Cannot determine with available metrics
        # Default to None (unknown) rather than guessing
        # This prevents false alerts based on incomplete data
        return {
            "is_active": None,  # Unknown - cannot determine reliably
            "reason": "Cannot determine active status from local metrics alone (no proposals/commits detected)",
            "source": "inference",
            "metrics_used": metrics_used if metrics_used else ["none_available"],
        }
