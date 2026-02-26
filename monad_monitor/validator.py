"""Validator health check logic"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from .config import ValidatorConfig
from .metrics import MetricsScraper

if TYPE_CHECKING:
    from monad_monitor.huginn import HuginnClient
    from monad_monitor.gmonads import GmonadsClient

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    """Health check result"""
    is_healthy: bool
    message: str
    metrics: Optional[Dict] = None
    block_height: Optional[float] = None
    peers: Optional[float] = None
    is_syncing: bool = False
    rpc_healthy: Optional[bool] = None
    warnings: List[str] = field(default_factory=list)
    criticals: List[str] = field(default_factory=list)  # Critical resource alerts
    is_active_validator: Optional[bool] = None  # True if in active set
    huginn_data: Optional[Dict] = None  # Huginn API uptime data
    system_metrics: Optional[Dict] = None  # CPU/RAM/Disk/TrieDB metrics


@dataclass
class SystemThresholds:
    """System resource threshold configuration"""
    cpu_warning: float = 90.0
    cpu_critical: float = 95.0
    memory_warning: float = 90.0
    memory_critical: float = 95.0
    disk_warning: float = 85.0
    disk_critical: float = 95.0


class ValidatorHealthChecker:
    """Check health of a single validator"""

    def __init__(
        self,
        validator: ValidatorConfig,
        timeout: int = 10,
        thresholds: Optional[SystemThresholds] = None,
        huginn_client: Optional["HuginnClient"] = None,
        gmonads_client: Optional["GmonadsClient"] = None,
    ):
        self.validator = validator
        self.scraper = MetricsScraper(
            metrics_url=validator.metrics_url,
            rpc_url=validator.rpc_url,
            timeout=timeout,
        )
        self.thresholds = thresholds or SystemThresholds()
        self.huginn_client = huginn_client
        self.gmonads_client = gmonads_client

    def check(
        self,
        last_block_commits: Optional[float] = None,
        last_execution_lagging: Optional[float] = None,
        last_ts_validation_fail: Optional[float] = None,
    ) -> Tuple[HealthStatus, Optional[float], Optional[float], Optional[float]]:
        """
        Perform health check on validator.

        Args:
            last_block_commits: Previous block commits count (for change detection)
            last_execution_lagging: Previous execution lagging count (for change detection)
            last_ts_validation_fail: Previous ts_validation_fail count (for change detection)

        Returns:
            Tuple of (HealthStatus, current_block_commits, current_execution_lagging, current_ts_validation_fail)
        """
        warnings = []

        # Fetch metrics
        metrics = self.scraper.get_monad_metrics()

        if "error" in metrics:
            return HealthStatus(
                is_healthy=False,
                message=f"Connection failed: {metrics['error']}",
                warnings=warnings,
                criticals=[],
            ), None, None, None

        # Check RPC health
        rpc_healthy = self.scraper.check_rpc_health()

        # Alert if RPC is unhealthy (but metrics are working)
        # This is rare but indicates RPC endpoint issues
        if not rpc_healthy:
            warnings.append("RPC endpoint not responding (metrics working)")

        # Fetch validator status from Huginn API (if available)
        # This is used for active set detection and uptime tracking
        validator_status = None
        is_active_validator = None
        huginn_data = None

        if self.huginn_client and self.validator.validator_secp:
            validator_status = self.scraper.get_validator_status(
                self.validator.validator_secp,
                huginn_client=self.huginn_client,
                network=self.validator.network,
                gmonads_client=self.gmonads_client,
            )
            is_active_validator = validator_status.get("is_active")
            huginn_data = validator_status.get("huginn_data")

        # Get ts_validation_fail for tracking
        current_ts_validation_fail = metrics.get("ts_validation_fail")

        # Check if node is producing blocks
        current_commits = metrics.get("block_commits")
        if current_commits is not None and last_block_commits is not None:
            if current_commits == last_block_commits:
                return HealthStatus(
                    is_healthy=False,
                    message="Node stopped producing blocks!",
                    metrics=metrics,
                    block_height=metrics.get("block_height"),
                    peers=metrics.get("peers"),
                    rpc_healthy=rpc_healthy,
                    warnings=warnings,
                    criticals=[],
                    is_active_validator=is_active_validator,
                    huginn_data=huginn_data,
                ), current_commits, last_execution_lagging, current_ts_validation_fail

        # Check execution lagging - only alert if INCREASING
        current_execution_lagging = metrics.get("execution_lagging")
        if current_execution_lagging is not None and current_execution_lagging > 0:
            # Check if value is increasing (indicates ongoing problem)
            if last_execution_lagging is not None:
                lag_increase = current_execution_lagging - last_execution_lagging
                if lag_increase > 0:
                    # Execution lagging is increasing - this is a problem
                    return HealthStatus(
                        is_healthy=False,
                        message=f"Execution lagging increasing: +{int(lag_increase)} (total: {int(current_execution_lagging)})",
                        metrics=metrics,
                        block_height=metrics.get("block_height"),
                        peers=metrics.get("peers"),
                        rpc_healthy=rpc_healthy,
                        warnings=warnings,
                        criticals=[],
                        is_active_validator=is_active_validator,
                        huginn_data=huginn_data,
                    ), current_commits, current_execution_lagging, current_ts_validation_fail
                # else: lagging is stable or decreasing, not a problem
            # First check - just add as warning, don't alert yet
            elif last_execution_lagging is None:
                warnings.append(f"Execution lagging detected: {int(current_execution_lagging)} (monitoring)")

        # Check ts_validation_fail - only alert if INCREASING for active validators
        # ts_validation_fail is a cumulative counter, so we track the delta
        if current_ts_validation_fail is not None and current_ts_validation_fail > 0:
            # If not in active set, skip warning entirely - this is expected behavior
            if is_active_validator is False:
                logger.info(
                    f"Skipping ts_validation_fail warning for inactive validator: {current_ts_validation_fail}"
                )
            elif is_active_validator is True and last_ts_validation_fail is not None:
                ts_increase = current_ts_validation_fail - last_ts_validation_fail
                if ts_increase > 0:
                    # ts_validation_fail is increasing - this is a problem
                    return HealthStatus(
                        is_healthy=False,
                        message=f"Timestamp validation fails increasing: +{int(ts_increase)} (total: {int(current_ts_validation_fail)})",
                        metrics=metrics,
                        block_height=metrics.get("block_height"),
                        peers=metrics.get("peers"),
                        rpc_healthy=rpc_healthy,
                        warnings=warnings,
                        criticals=[],
                        is_active_validator=is_active_validator,
                        huginn_data=huginn_data,
                    ), current_commits, current_execution_lagging, current_ts_validation_fail
            elif last_ts_validation_fail is None:
                # First check - just add as warning, don't alert yet
                warnings.append(f"Timestamp validation fails detected: {int(current_ts_validation_fail)} (monitoring)")

        # Check if catching up (blocksync)
        blocksync = metrics.get("blocksync")
        is_syncing = bool(blocksync and blocksync > 0)

        # Collect system metrics (CPU/RAM/Disk/TrieDB) for reports
        system_metrics = None
        criticals = []
        if self.validator.node_exporter_url:
            system_metrics = self.check_system_metrics()
            system_warnings, system_criticals = self._check_system_thresholds(system_metrics)
            warnings.extend(system_warnings)
            criticals.extend(system_criticals)

        # Get basic stats
        block_height = metrics.get("block_height")
        peers = metrics.get("peers")
        syncing_flag = metrics.get("syncing")
        sync_status = "syncing" if syncing_flag or is_syncing else "synced"

        # Build status message
        msg_parts = []
        if block_height is not None:
            msg_parts.append(f"Height: {int(block_height)}")
        if peers is not None:
            msg_parts.append(f"Peers: {int(peers)}")
        msg_parts.append(sync_status)

        # Add epoch/round if available
        epoch = metrics.get("current_epoch")
        round_num = metrics.get("current_round")
        if epoch is not None:
            msg_parts.append(f"Epoch: {int(epoch)}")
        if round_num is not None:
            msg_parts.append(f"Round: {int(round_num)}")

        if is_syncing:
            message = f"Catching up (blocksync): {', '.join(msg_parts)}"
        else:
            message = f"OK - {', '.join(msg_parts)}"

        # Add warnings/criticals to message if any
        if warnings:
            message += f" [Warnings: {len(warnings)}]"
        if criticals:
            message += f" [Criticals: {len(criticals)}]"

        return HealthStatus(
            is_healthy=True,
            message=message,
            metrics=metrics,
            block_height=block_height,
            peers=peers,
            is_syncing=is_syncing,
            rpc_healthy=rpc_healthy,
            warnings=warnings,
            criticals=criticals,
            is_active_validator=is_active_validator,
            huginn_data=huginn_data,
            system_metrics=system_metrics,
        ), current_commits, current_execution_lagging, current_ts_validation_fail

    def _check_system_thresholds(self, system_metrics: Optional[Dict] = None) -> Tuple[List[str], List[str]]:
        """Check system metrics against thresholds and return (warnings, criticals)"""
        warnings = []
        criticals = []

        if not system_metrics:
            system_metrics = self.check_system_metrics()

        if not system_metrics:
            return warnings, criticals

        # CPU check
        cpu_percent = system_metrics.get("cpu_used_percent")
        if cpu_percent is not None:
            if cpu_percent >= self.thresholds.cpu_critical:
                criticals.append(f"CPU critical: {cpu_percent:.1f}%")
            elif cpu_percent >= self.thresholds.cpu_warning:
                warnings.append(f"CPU warning: {cpu_percent:.1f}%")

        # Memory check
        mem_percent = system_metrics.get("mem_percent")
        if mem_percent is not None:
            if mem_percent >= self.thresholds.memory_critical:
                criticals.append(f"Memory critical: {mem_percent:.1f}%")
            elif mem_percent >= self.thresholds.memory_warning:
                warnings.append(f"Memory warning: {mem_percent:.1f}%")

        # Disk check
        disk_percent = system_metrics.get("disk_percent")
        if disk_percent is not None:
            if disk_percent >= self.thresholds.disk_critical:
                criticals.append(f"Disk critical: {disk_percent:.1f}%")
            elif disk_percent >= self.thresholds.disk_warning:
                warnings.append(f"Disk warning: {disk_percent:.1f}%")

        return warnings, criticals

    def check_system_metrics(self) -> Dict:
        """Fetch system metrics from node exporter if available"""
        if not self.validator.node_exporter_url:
            return {}

        return self.scraper.get_system_metrics(self.validator.node_exporter_url)
