"""Monad Validator Monitor - Main Entry Point"""

import signal
import sys
import time
from datetime import datetime
from typing import Dict, Any, Optional

from .alerts import AlertHandler
from .config import (
    load_config,
    load_validators,
    load_huginn_config,
    load_gmonads_config,
    validate_config,
    validate_validators,
    ConfigValidationError,
)
from .cross_validation import CrossValidator
from .dashboard_server import DashboardServer
from .gmonads import GmonadsClient
from .health_report import HealthReporter
from .health_server import HealthServer
from .huginn import HuginnClient
from .logger import init_logger, get_logger, debug, info, warning, error
from .state_machine import ValidatorStateMachine, ValidatorState
from .validator import ValidatorHealthChecker, SystemThresholds

# Constants
MAX_METRICS_HISTORY = 100  # Maximum entries per validator to prevent unbounded growth
STATE_FILE = "validator_state.json"  # State persistence file
STATE_DIR = "/app/state"  # Directory for state persistence (Docker volume mount point)

# Global state for graceful shutdown
running = True
health_server: Optional[HealthServer] = None
dashboard_server: Optional[DashboardServer] = None

def signal_handler(sig, frame):
    """Handle shutdown signals gracefully"""
    global running
    info("Shutdown signal received...")
    running = False


def main():
    """Main entry point for the monitor"""
    global running, health_server, dashboard_server

    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Load configuration
    try:
        config = load_config()
        validators = load_validators()
    except Exception as e:
        print(f"Configuration error: {e}")
        sys.exit(1)

    if not validators:
        print("No enabled validators found in configuration")
        sys.exit(1)

    # Initialize logger with config
    log_level = config.get("logging", {}).get("level", "INFO")
    log_file = config.get("logging", {}).get("file")
    init_logger(level=log_level, log_file=log_file)
    logger = get_logger()

    # Validate configuration at startup (clear error messages for misconfig)
    try:
        validate_config(config)
        validate_validators(validators)
    except ConfigValidationError as e:
        error(f"Configuration validation failed:\n{e}")
        sys.exit(1)

    # Initialize Health Server (HTTP endpoints for health checks and metrics)
    health_server_config = config.get("health_server", {})
    health_server_enabled = health_server_config.get("enabled", True)
    health_server_port = health_server_config.get("port", 8181)
    health_server_host = health_server_config.get("host", "0.0.0.0")

    if health_server_enabled:
        health_server = HealthServer(host=health_server_host, port=health_server_port)
        try:
            health_server.start()
            info(f"Health server started on {health_server_host}:{health_server_port}")
        except OSError as e:
            warning(f"Failed to start health server on port {health_server_port}: {e}")
            warning("Continuing without health server...")
            health_server = None

    # Initialize Dashboard Server (Web UI on port 8282)
    dashboard_server_config = config.get("dashboard_server", {})
    dashboard_server_enabled = dashboard_server_config.get("enabled", True)
    dashboard_server_port = dashboard_server_config.get("port", 8282)
    dashboard_server_host = dashboard_server_config.get("host", "127.0.0.1")

    if dashboard_server_enabled:
        dashboard_server = DashboardServer(host=dashboard_server_host, port=dashboard_server_port)
        try:
            dashboard_server.start()
            info(f"Dashboard server started on {dashboard_server_host}:{dashboard_server_port}")
        except Exception as e:
            warning(f"Failed to start dashboard server on port {dashboard_server_port}: {e}")
            warning("Continuing without dashboard server...")
            dashboard_server = None

    # Initialize Huginn client for external validator status verification
    huginn_config = load_huginn_config()
    huginn_client = None
    if huginn_config.enabled:
        huginn_client = HuginnClient(config=huginn_config)
        info("Huginn API enabled - external validator status verification active")
    else:
        info("Huginn API disabled - using inference for validator status")

    # Initialize gmonads client for network-wide metrics
    gmonads_config = load_gmonads_config()
    gmonads_client = None
    if gmonads_config.enabled:
        gmonads_client = GmonadsClient(config=gmonads_config)
        info("gmonads API enabled - cross-validation and extended reports active")
    else:
        info("gmonads API disabled - cross-validation and extended reports unavailable")

    # Initialize cross-validator (requires both clients)
    cross_validator = None
    if huginn_client and gmonads_client:
        cross_validator = CrossValidator(huginn_client, gmonads_client)
        info("Cross-validation enabled - comparing Huginn and gmonads data")

    # Initialize components
    alerts = AlertHandler(
        telegram_token=config["telegram"]["token"],
        telegram_chat_id=config["telegram"]["chat_id"],
        pushover_user_key=config["pushover"].get("user_key"),
        pushover_app_token=config["pushover"].get("app_token"),
        discord_webhook_url=config.get("discord", {}).get("webhook_url"),
    )


    health_reporter = HealthReporter(
        alerts=alerts,
        report_interval=config["monitoring"].get("health_report_interval", 3600),
        extended_report_interval=config["monitoring"].get("extended_report_interval", 21600),  # 6 hours
    )

    # Initialize system thresholds from config
    thresholds_config = config.get("thresholds", {})
    thresholds = SystemThresholds(
        cpu_warning=thresholds_config.get("cpu_warning", 90),
        cpu_critical=thresholds_config.get("cpu_critical", 95),
        memory_warning=thresholds_config.get("memory_warning", 90),
        memory_critical=thresholds_config.get("memory_critical", 95),
        disk_warning=thresholds_config.get("disk_warning", 85),
        disk_critical=thresholds_config.get("disk_critical", 95),
    )

    # State tracking for each validator
    states: Dict[str, Dict] = {}
    state_machines: Dict[str, ValidatorStateMachine] = {}

    # Ensure state directory exists (for Docker volume persistence)
    import os
    state_dir = STATE_DIR
    if not os.path.exists(state_dir):
        try:
            os.makedirs(state_dir, exist_ok=True)
            debug(f"Created state directory: {state_dir}")
        except OSError as e:
            warning(f"Failed to create state directory {state_dir}: {e}. Using current directory.")
            state_dir = "."

    # Load persisted state for each validator
    for v in validators:
        states[v.name] = {
            "fails": 0,
            "alert_active": False,
            "last_commits": None,
            "last_height": None,
            "last_peers": None,
            "warning_counts": {},  # Track warning occurrences
            "critical_counts": {},  # Track critical resource occurrences
            "last_execution_lagging": None,  # Track execution lagging for increase detection
            "last_ts_validation_fail": None,  # Track ts_validation_fail for increase detection
            "last_huginn_timeout_count": None,  # Track Huginn timeout count (network-visible timeouts)
        }
        # Sanitize validator name for filename (replace spaces and special chars)
        safe_name = v.name.replace(" ", "_").replace("/", "_").replace("\\", "_")
        state_file = os.path.join(state_dir, f"state_{safe_name}.json")
        loaded_machine = ValidatorStateMachine.load_state(state_file)
        if loaded_machine.validator_name == v.name:
            state_machines[v.name] = loaded_machine
            info(f"Loaded persisted state for {v.name}: {loaded_machine.current_state.value}")
        else:
            # No valid persisted state, create new
            state_machines[v.name] = ValidatorStateMachine(validator_name=v.name)
            debug(f"Created new state machine for {v.name}")

    # Metrics data for extended reports
    metrics_data: Dict[str, Dict] = {}

    # Send startup notification
    health_reporter.send_startup_report(validators)
    info(f"Monitor started - {len(validators)} validators | Log level: {log_level}")

    # Main monitoring loop
    try:
        while running:
            timestamp = datetime.now().strftime("%H:%M:%S")
            all_healthy = True
            health_server_validators: Dict[str, Dict[str, Any]] = {}

            for validator in validators:
                if not running:
                    break

                state = states[validator.name]
                state_machine = state_machines[validator.name]
                checker = ValidatorHealthChecker(
                    validator=validator,
                    timeout=config["monitoring"].get("timeout", 10),
                    thresholds=thresholds,
                    huginn_client=huginn_client,
                    gmonads_client=gmonads_client,
                )

                # Perform health check
                health_status, current_commits, current_execution_lagging, current_ts_validation_fail = checker.check(
                    state["last_commits"],
                    state.get("last_execution_lagging"),
                    state.get("last_ts_validation_fail"),
                )
                state["last_commits"] = current_commits
                state["last_execution_lagging"] = current_execution_lagging
                state["last_ts_validation_fail"] = current_ts_validation_fail

                # Update state with latest metrics
                if health_status.metrics:
                    state["last_height"] = health_status.block_height
                    state["last_peers"] = health_status.peers

                    # Store metrics for extended reports
                    metrics_data[validator.name] = {
                        "is_active_validator": health_status.is_active_validator,
                        "proposed_blocks": health_status.metrics.get("proposals"),
                        "signed_blocks": health_status.metrics.get("block_commits"),
                        "local_timeout": health_status.metrics.get("local_timeout"),
                        "huginn_data": health_status.huginn_data,
                        "system_metrics": health_status.system_metrics,  # CPU/RAM/Disk/TrieDB
                    }

                    # Log Huginn API data if available (DEBUG level)
                    if health_status.huginn_data:
                        h = health_status.huginn_data
                        debug(
                            f"Huginn [{validator.name}]: is_active={h.get('is_active')}, "
                            f"is_ever_active={h.get('is_ever_active')}, "
                            f"round_diff={h.get('round_diff')}, "
                            f"uptime={h.get('uptime_percent')}%, "
                            f"total_events={h.get('total_events')}"
                        )

                    # Update state machine with validator status
                    is_active = health_status.is_active_validator
                    is_ever_active = False
                    if health_status.huginn_data:
                        is_ever_active = health_status.huginn_data.get("is_ever_active", False)

                    # Initialize state machine with correct state on first check
                    # This prevents false "ENTERED ACTIVE SET" alerts on restart
                    if state_machine.current_state == ValidatorState.NEW and is_ever_active:
                        if is_active:
                            state_machine.current_state = ValidatorState.ACTIVE
                        else:
                            state_machine.current_state = ValidatorState.INACTIVE
                        state_machine._state_entered_at = time.time()
                        debug(f"Initialized state machine for {validator.name} as {state_machine.current_state.value}")
                        transition = None
                    else:
                        # If we don't have Huginn data, infer is_ever_active from current state
                        if is_ever_active is False and state_machine.current_state != ValidatorState.NEW:
                            is_ever_active = True

                        transition = state_machine.update(
                            is_active=is_active if is_active is not None else False,
                            is_ever_active=is_ever_active,
                            metadata={"round_diff": health_status.huginn_data.get("round_diff")} if health_status.huginn_data else {}
                        )

                    # Handle state transitions with alerts (Telegram + Discord)
                    if transition and transition.is_significant():
                        alert_msg = transition.get_alert_message()
                        alerts.alert_info(alert_msg)
                        info(f"State transition for {validator.name}: {transition.from_state.value} -> {transition.to_state.value}")

                    # Check for Huginn timeout_count increase (network-visible timeouts)
                    # This is the REAL timeout that matters - if Huginn sees timeouts, validator missed rounds
                    if health_status.huginn_data:
                        huginn_timeout_count = health_status.huginn_data.get("timeout_count", 0)
                        last_huginn_timeout = state.get("last_huginn_timeout_count")

                        if last_huginn_timeout is not None and huginn_timeout_count > last_huginn_timeout:
                            timeout_increase = huginn_timeout_count - last_huginn_timeout
                            if timeout_increase > 0:
                                error(f"❌ {validator.name}: Network timeout detected (Huginn): +{timeout_increase} (total: {huginn_timeout_count})")
                                alert_success = alerts.alert_critical(
                                    f"*{validator.name}*\n\n"
                                    f"⚠️ Network Timeout Detected\n\n"
                                    f"Validator missed {timeout_increase} round(s) as seen by network.\n"
                                    f"Total timeouts: {huginn_timeout_count}",
                                    validator_name=validator.name,
                                )
                                if not alert_success:
                                    error(f"Failed to send Huginn timeout alert for {validator.name}")

                        state["last_huginn_timeout_count"] = huginn_timeout_count
                    else:
                        # Huginn unavailable - no local_timeout fallback
                        # local_timeout metric tracks OTHER nodes' timeouts, not our validator's status
                        # We rely on gmonads fallback (already implemented) and local health metrics
                        debug(f"Huginn unavailable for {validator.name}, relying on gmonads and local metrics")

                # Update health server validator data
                health_server_validators[validator.name] = {
                    "state": state_machine.current_state.value,
                    "healthy": health_status.is_healthy,
                    "height": state.get("last_height"),
                    "peers": state.get("last_peers"),
                    "fails": state["fails"],
                    "huginn_data": health_status.huginn_data,
                    "last_check": time.time(),  # Timestamp for last check
                }

                # Handle warnings (non-critical alerts)
                if health_status.warnings:
                    for warn_msg in health_status.warnings:
                        # Track warning occurrences
                        warning_key = warn_msg.split(":")[0]  # e.g., "CPU", "Memory", "Disk"
                        state["warning_counts"][warning_key] = state["warning_counts"].get(warning_key, 0) + 1

                        # Send warning alert after 3 consecutive occurrences
                        if state["warning_counts"][warning_key] == 3:
                            alerts.alert_warning(f"*{validator.name}*\n\n{warn_msg}")
                            state["warning_counts"][warning_key] = -10  # Cooldown to prevent spam
                else:
                    # Reset warning counts on healthy check
                    for key in list(state["warning_counts"].keys()):
                        if state["warning_counts"][key] < 0:
                            state["warning_counts"][key] += 1
                        else:
                            state["warning_counts"][key] = 0

                # Handle critical resource alerts (Telegram + Pushover + Discord)
                if health_status.criticals:
                    for critical_msg in health_status.criticals:
                        # Track critical occurrences
                        critical_key = critical_msg.split(":")[0]  # e.g., "CPU", "Memory", "Disk"
                        state["critical_counts"][critical_key] = state["critical_counts"].get(critical_key, 0) + 1

                        # Send critical alert after 2 consecutive occurrences (faster than warnings)
                        if state["critical_counts"][critical_key] == 2:
                            alerts.alert_critical(
                                f"*{validator.name}*\n\n{critical_msg}",
                                validator_name=validator.name
                            )
                            state["critical_counts"][critical_key] = -10  # Cooldown to prevent spam
                else:
                    # Reset critical counts on healthy check
                    for key in list(state["critical_counts"].keys()):
                        if state["critical_counts"][key] < 0:
                            state["critical_counts"][key] += 1
                        else:
                            state["critical_counts"][key] = 0

                if health_status.is_healthy:
                    state["fails"] = 0

                    # Recovery notification (Telegram + Discord)
                    if state["alert_active"]:
                        recovery_msg = f"✅ *{validator.name} RECOVERED*\n\n{health_status.message}"
                        alerts.alert_info(recovery_msg)
                        alerts.reset_pushover_cooldown(validator.name)
                        state["alert_active"] = False

                    # Log healthy status (INFO level - concise format)
                    # Format height with thousand separators
                    height_formatted = f"{state.get('last_height'):,}" if state.get('last_height') else "N/A"
                    peers_formatted = state.get('last_peers', 'N/A')
                    info(f"✅ {validator.name}: In-sync · Height: {height_formatted} · Peers: {peers_formatted}")

                    # Log detailed Huginn data at DEBUG level
                    if health_status.huginn_data:
                        h = health_status.huginn_data
                        debug(
                            f"Huginn [{validator.name}]: round_diff={h.get('round_diff')}, "
                            f"uptime={h.get('uptime_percent')}%, total_events={h.get('total_events')}"
                        )
                else:
                    all_healthy = False
                    state["fails"] += 1
                    threshold = config["monitoring"].get("alert_threshold", 3)

                    error(f"❌ {validator.name}: {health_status.message} ({state['fails']}/{threshold})")

                    # Trigger alert if threshold reached
                    if state["fails"] >= threshold and not state["alert_active"]:
                        alert_success = alerts.alert_critical(
                            f"*{validator.name}*\n\n{health_status.message}",
                            validator_name=validator.name,
                        )
                        # Only mark alert_active if alert was actually sent
                        # This prevents missing alerts due to send failures
                        if alert_success:
                            state["alert_active"] = True
                        else:
                            error(f"Failed to send CRITICAL alert for {validator.name} - will retry next cycle")

                # Brief pause between validator checks
                time.sleep(1)

            # Update health server with overall status
            if health_server:
                health_server.update_status(is_healthy=all_healthy, validators=health_server_validators)

            # Update dashboard server with validator data
            if dashboard_server:
                health_status = health_server.get_health_status() if health_server else None
                # Get network from first validator (all should be same network)
                first_validator = validators[0] if validators else None
                network = first_validator.network if first_validator else "testnet"
                dashboard_server.update_validators(
                    validators=health_server_validators,
                    status="healthy" if all_healthy else "unhealthy",
                    uptime_seconds=health_status.uptime_seconds if health_status else 0.0,
                    network=network,
                )

            # Check if it's time for extended health report (6-hour detailed report)
            health_reporter.maybe_send_extended_report(validators, states, metrics_data)

            # Memory cleanup: Remove stale entries from metrics_data
            # (validators that were removed from config)
            configured_names = {v.name for v in validators}
            stale_names = set(metrics_data.keys()) - configured_names
            for stale_name in stale_names:
                del metrics_data[stale_name]
                debug(f"Removed stale metrics entry for: {stale_name}")

            # Retry any failed critical alerts
            retried = alerts.retry_failed_alerts()
            if retried > 0:
                info(f"Retried {retried} failed alert(s)")

            # Wait for next cycle with interruptible sleep
            # This allows quick response to SIGTERM by checking running flag every second
            if running:
                sleep_interval = config["monitoring"].get("check_interval", 60)
                slept = 0
                while running and slept < sleep_interval:
                    time.sleep(1)
                    slept += 1

    finally:
        # Graceful shutdown
        info("Initiating graceful shutdown...")

        # Save state machines before stopping servers
        for name, machine in state_machines.items():
            # Sanitize validator name for filename (replace spaces and special chars)
            safe_name = name.replace(" ", "_").replace("/", "_").replace("\\", "_")
            state_file = os.path.join(state_dir, f"state_{safe_name}.json")
            if machine.save_state(state_file):
                info(f"Saved state for {name}: {machine.current_state.value}")
            else:
                warning(f"Failed to save state for {name}")

        if dashboard_server:
            info("Stopping dashboard server...")
            dashboard_server.stop()

        if health_server:
            info("Stopping health server...")
            health_server.stop()

        # Send shutdown notification
        health_reporter.send_shutdown_report()
        info("Monitor stopped.")


if __name__ == "__main__":
    main()
