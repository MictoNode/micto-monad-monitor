"""Periodic health report generation"""

import time
from datetime import datetime
from typing import Dict, List, Optional

from .alerts import AlertHandler
from .config import ValidatorConfig


class HealthReporter:
    """Generate and send periodic health reports"""

    def __init__(
        self,
        alerts: AlertHandler,
        report_interval: int = 3600,
        extended_report_interval: int = 21600,  # 6 hours
    ):
        self.alerts = alerts
        self.report_interval = report_interval
        self.extended_report_interval = extended_report_interval
        self.last_report_time = 0
        self.last_extended_report_time = 0

    def maybe_send_report(
        self,
        validators: List[ValidatorConfig],
        states: Dict[str, Dict],
    ) -> bool:
        """
        Send health report if interval has elapsed.

        NOTE: This method is currently not used in the main monitoring loop.
        The main loop uses maybe_send_extended_report() instead, which provides
        more detailed metrics. This method is kept for potential future use
        cases where a simpler, lighter-weight report is needed.

        Args:
            validators: List of validator configurations
            states: Current validator states

        Returns:
            True if report was sent, False otherwise
        """
        current_time = time.time()

        if current_time - self.last_report_time < self.report_interval:
            return False

        self.last_report_time = current_time
        self._send_report(validators, states)
        return True

    def maybe_send_extended_report(
        self,
        validators: List[ValidatorConfig],
        states: Dict[str, Dict],
        metrics_data: Optional[Dict[str, Dict]] = None,
    ) -> bool:
        """
        Send extended health report if interval has elapsed.

        Extended report includes detailed block production metrics.

        Args:
            validators: List of validator configurations
            states: Current validator states
            metrics_data: Optional metrics data for each validator

        Returns:
            True if report was sent, False otherwise
        """
        current_time = time.time()

        if current_time - self.last_extended_report_time < self.extended_report_interval:
            return False

        self.last_extended_report_time = current_time
        self._send_extended_report(validators, states, metrics_data)
        return True

    def _send_report(
        self,
        validators: List[ValidatorConfig],
        states: Dict[str, Dict],
    ) -> None:
        """Generate and send health report"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        report_lines = [
            f"📊 *Monad Health Report*",
            f"⏰ {timestamp}",
            "",
        ]

        healthy_count = 0
        unhealthy_count = 0

        for validator in validators:
            state = states.get(validator.name, {})
            is_healthy = not state.get("alert_active", False)

            if is_healthy:
                healthy_count += 1
                status_emoji = "✅"
            else:
                unhealthy_count += 1
                status_emoji = "❌"

            report_lines.append(
                f"{status_emoji} *{validator.name}*"
            )
            report_lines.append(f"   Host: `{validator.host}`")

            # Add last known status
            last_height = state.get("last_height")
            last_peers = state.get("last_peers")

            if last_height is not None:
                report_lines.append(f"   Height: {int(last_height)}")
            if last_peers is not None:
                report_lines.append(f"   Peers: {int(last_peers)}")

            report_lines.append("")

        # Summary
        report_lines.extend([
            "━━━━━━━━━━━━━━━━━━━━",
            f"📈 *Summary:*",
            f"   ✅ Healthy: {healthy_count}",
            f"   ❌ Unhealthy: {unhealthy_count}",
        ])

        report = "\n".join(report_lines)
        self.alerts.send_telegram(report)
        # Also send to Discord if configured
        self.alerts.send_discord(
            message=report.replace("*", "").replace("`", ""),  # Discord doesn't use Markdown
            title="📊 Monad Health Report",
            color=0x3498db,  # Blue
        )

    def _send_extended_report(
        self,
        validators: List[ValidatorConfig],
        states: Dict[str, Dict],
        metrics_data: Optional[Dict[str, Dict]] = None,
    ) -> None:
        """Generate and send extended health report with block production metrics"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        report_lines = [
            f"📊 *Monad Extended Health Report*",
            f"⏰ {timestamp}",
            "",
        ]

        healthy_count = 0
        unhealthy_count = 0

        for validator in validators:
            state = states.get(validator.name, {})
            is_healthy = not state.get("alert_active", False)

            if is_healthy:
                healthy_count += 1
                status_emoji = "✅"
            else:
                unhealthy_count += 1
                status_emoji = "❌"

            report_lines.append(f"{status_emoji} *{validator.name}*")
            report_lines.append(f"   Host: `{validator.host}`")

            # Basic metrics
            last_height = state.get("last_height")
            last_peers = state.get("last_peers")

            if last_height is not None:
                report_lines.append(f"   Height: {int(last_height)}")
            if last_peers is not None:
                report_lines.append(f"   Peers: {int(last_peers)}")

            # Enhanced block metrics from metrics_data
            if metrics_data:
                v_metrics = metrics_data.get(validator.name, {})

                # Active validator status
                is_active = v_metrics.get("is_active_validator")
                if is_active is not None:
                    active_emoji = "🟢" if is_active else "⚪"
                    active_text = "Active" if is_active else "Inactive"
                    report_lines.append(f"   {active_emoji} Status: {active_text}")

                # Huginn API uptime data (if available)
                huginn_data = v_metrics.get("huginn_data")
                if huginn_data:
                    uptime_percent = huginn_data.get("uptime_percent")
                    finalized_count = huginn_data.get("finalized_count")
                    timeout_count = huginn_data.get("timeout_count")
                    total_events = huginn_data.get("total_events")

                    if uptime_percent is not None:
                        report_lines.append(f"   📊 Uptime: {uptime_percent}%")
                    if finalized_count is not None and total_events is not None:
                        report_lines.append(f"   ✅ Finalized: {finalized_count}/{total_events}")
                    if timeout_count is not None and timeout_count > 0:
                        report_lines.append(f"   ⏱️ Timeouts: {timeout_count}")

                    # Last round info
                    last_round = huginn_data.get("last_round")
                    if last_round is not None:
                        report_lines.append(f"   🔢 Last Round: {last_round}")

                # Block production metrics
                proposed = v_metrics.get("proposed_blocks")
                signed = v_metrics.get("signed_blocks")
                missed = v_metrics.get("missed_blocks")

                if proposed is not None:
                    report_lines.append(f"   📤 Proposed: {int(proposed)}")
                if signed is not None:
                    report_lines.append(f"   ✍️ Signed: {int(signed)}")
                if missed is not None:
                    report_lines.append(f"   ❌ Missed: {int(missed)}")

                # System metrics (CPU/RAM/Disk/TrieDB)
                sys_metrics = v_metrics.get("system_metrics")
                if sys_metrics:
                    # CPU
                    cpu_percent = sys_metrics.get("cpu_used_percent")
                    if cpu_percent is not None:
                        cpu_emoji = "🔴" if cpu_percent >= 90 else "🟡" if cpu_percent >= 80 else "🟢"
                        report_lines.append(f"   {cpu_emoji} CPU: {cpu_percent:.1f}%")

                    # Memory
                    mem_percent = sys_metrics.get("mem_percent")
                    if mem_percent is not None:
                        mem_emoji = "🔴" if mem_percent >= 90 else "🟡" if mem_percent >= 80 else "🟢"
                        report_lines.append(f"   {mem_emoji} RAM: {mem_percent:.1f}%")

                    # Disk
                    disk_percent = sys_metrics.get("disk_percent")
                    if disk_percent is not None:
                        disk_emoji = "🔴" if disk_percent >= 90 else "🟡" if disk_percent >= 80 else "🟢"
                        report_lines.append(f"   {disk_emoji} Disk: {disk_percent:.1f}%")

                    # TrieDB
                    triedb = sys_metrics.get("triedb", {})
                    if triedb:
                        triedb_percent = triedb.get("used_percent")
                        if triedb_percent is not None:
                            triedb_emoji = "🔴" if triedb_percent >= 80 else "🟡" if triedb_percent >= 60 else "🟢"
                            report_lines.append(f"   {triedb_emoji} TrieDB: {triedb_percent:.1f}%")

            report_lines.append("")

        # Summary
        report_lines.extend([
            "━━━━━━━━━━━━━━━━━━━━",
            f"📈 *Summary:*",
            f"   ✅ Healthy: {healthy_count}",
            f"   ❌ Unhealthy: {unhealthy_count}",
        ])

        report = "\n".join(report_lines)
        self.alerts.send_telegram(report)
        # Also send to Discord if configured
        self.alerts.send_discord(
            message=report.replace("*", "").replace("`", ""),  # Discord doesn't use Markdown
            title="📊 Monad Extended Health Report",
            color=0x2ecc71,  # Green for extended report
        )

    def send_startup_report(
        self, validators: List[ValidatorConfig]
    ) -> None:
        """Send startup notification (Telegram + Discord)"""
        msg_lines = [
            "🟢 *Monad Monitor Started*",
            "",
            f"Monitoring {len(validators)} validator(s):",
            "",
        ]

        for v in validators:
            msg_lines.append(f"• {v.name} (`{v.host}`)")

        msg = "\n".join(msg_lines)
        self.alerts.send_telegram(msg)
        # Also send to Discord if configured
        self.alerts.send_discord(
            message=msg.replace("*", "").replace("`", ""),
            title="🟢 Monad Monitor Started",
            color=0x2ecc71,  # Green
        )

    def send_shutdown_report(self) -> None:
        """Send shutdown notification (Telegram + Discord)"""
        msg = "🔴 *Monad Monitor Stopped*"
        self.alerts.send_telegram(msg)
        # Also send to Discord if configured
        self.alerts.send_discord(
            message="Monad Monitor Stopped",
            title="🔴 Monad Monitor Stopped",
            color=0xe74c3c,  # Red
        )
