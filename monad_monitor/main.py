"""Monad Validator Monitor - Main Entry Point"""

import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple

from .alerts import AlertHandler, escape_markdown
from .config import (
    load_config,
    load_validators,
    load_huginn_config,
    load_gmonads_config,
    load_updates_config,
    validate_config,
    validate_validators,
    ConfigValidationError,
    ValidatorConfig,
)
from .cross_validation import CrossValidator
from .dashboard_server import DashboardServer
from .gmonads import GmonadsClient
from .health_report import HealthReporter
from .health_server import DEFAULT_STALENESS_THRESHOLD_SECONDS, HealthServer
from .huginn import HuginnClient, ValidatorSetState
from .logger import init_logger, get_logger, debug, info, warning, error
from .state_machine import ValidatorStateMachine, ValidatorState, StateTransition
from .validator import ValidatorHealthChecker, HealthStatus, SystemThresholds
from .api_server import APIServer
from .version_check import VersionChecker

# Constants
MAX_METRICS_HISTORY = 100  # Maximum entries per validator to prevent unbounded growth
STATE_FILE = "validator_state.json"  # State persistence file
STATE_DIR = "/app/state"  # Directory for state persistence (Docker volume mount point)

# Global state for graceful shutdown
running = True
health_server: Optional[HealthServer] = None
dashboard_server: Optional[DashboardServer] = None

def timeout_increase_to_report(
    last_timeout_count: Optional[int], current_timeout_count: int, threshold: int
) -> int:
    """Return the Huginn timeout increase if it meets the alert threshold, else 0.

    A None baseline (first check after start) never reports. A flat or
    decreasing count never reports. The alert fires only when the increase
    between checks is >= threshold (threshold 1 = alert on any missed round).
    """
    if last_timeout_count is None or current_timeout_count <= last_timeout_count:
        return 0
    increase = current_timeout_count - last_timeout_count
    return increase if increase >= threshold else 0


def handle_validator_failure(
    validator: ValidatorConfig,
    state: Dict[str, Any],
    validators_payload: Dict[str, Dict[str, Any]],
    exc: Exception,
    state_label: str,
) -> None:
    """Record a validator whose cycle raised, keeping the monitor loop alive.

    Without this the exception would propagate out of the `for` loop: every
    other validator stops being checked, the monitor process exits and the
    container restarts. The validator is published as unhealthy so it stays
    visible on the dashboard instead of silently vanishing from the payload.
    """
    error(f"{validator.name}: check cycle raised unexpectedly: {exc}")
    debug(traceback.format_exc())
    state["fails"] += 1
    validators_payload[validator.name] = {
        "state": state_label,
        "healthy": False,
        "height": state.get("last_height"),
        "peers": state.get("last_peers"),
        "fails": state["fails"],
        "huginn_data": None,
        "last_check": time.time(),
        "network": validator.network,
        "system_metrics": None,
        "block_production": None,
        "warnings": [],
        "criticals": [],
        "rpc_healthy": True,
        "error": "health check failed unexpectedly (see logs)",
    }


def apply_active_set_transition(
    state_machine: ValidatorStateMachine,
    validator_name: str,
    is_active: Optional[bool],
    is_ever_active: bool,
) -> Optional[StateTransition]:
    """Feed the active-set verdict into the state machine, or hold on unknown.

    A None verdict means no source could say whether the validator is in the
    active set (both APIs quiet, and no local evidence of participation). That
    used to be coerced to False, which sent a spurious "LEFT ACTIVE SET" alert
    and left the active-only alerts muted until the sources recovered. Holding
    the state is the honest answer; the consequence is that an active-set
    transition now requires a verified verdict, so a deployment that disables
    Huginn and gmonads no longer guesses set membership (its validators are
    still covered by the health/resource alerts).
    """
    if is_active is None:
        debug(f"{validator_name}: active set verdict unknown - keeping current state")
        return None

    # Initialize the state machine with the correct state on the first verdict:
    # this is what prevents a false "ENTERED ACTIVE SET" alert after a restart.
    if state_machine.current_state == ValidatorState.NEW and is_ever_active:
        state_machine.current_state = (
            ValidatorState.ACTIVE if is_active else ValidatorState.INACTIVE
        )
        state_machine._state_entered_at = time.time()
        debug(
            f"Initialized state machine for {validator_name} as "
            f"{state_machine.current_state.value}"
        )
        return None

    # Without Huginn data, a non-NEW machine has necessarily been active before
    if not is_ever_active and state_machine.current_state != ValidatorState.NEW:
        is_ever_active = True

    return state_machine.update(
        is_active=is_active,
        is_ever_active=is_ever_active,
        metadata={},
    )


def handle_huginn_timeout(
    state: Dict[str, Any],
    validator_name: str,
    huginn_data: Dict[str, Any],
    threshold: int,
    alerts: AlertHandler,
) -> bool:
    """Alert on a network-visible Huginn timeout increase.

    The baseline only advances when the alert actually went out - the same rule
    the health-CRITICAL path uses. Advancing it unconditionally loses the alert
    for that increase forever if every channel was down at that moment.
    """
    huginn_timeout_count = huginn_data.get("timeout_count", 0)
    last_huginn_timeout = state.get("last_huginn_timeout_count")

    timeout_increase = timeout_increase_to_report(
        last_huginn_timeout, huginn_timeout_count, threshold
    )
    if timeout_increase > 0:
        error(
            f"❌ {validator_name}: Network timeout detected (Huginn): "
            f"+{timeout_increase} (total: {huginn_timeout_count})"
        )
        alert_success = alerts.alert_critical(
            f"*{escape_markdown(validator_name)}*\n\n"
            f"⚠️ Network Timeout Detected\n\n"
            f"Validator missed {timeout_increase} round(s) as seen by network.\n"
            f"Total timeouts: {huginn_timeout_count}",
            validator_name=validator_name,
        )
        if not alert_success:
            error(
                f"Failed to send Huginn timeout alert for {validator_name} "
                f"- will retry next cycle"
            )
            return False
        state["last_huginn_timeout_count"] = huginn_timeout_count
        return True

    if last_huginn_timeout is not None and huginn_timeout_count > last_huginn_timeout:
        info(
            f"⏱️ {validator_name}: Huginn timeout "
            f"+{huginn_timeout_count - last_huginn_timeout} "
            f"(total: {huginn_timeout_count}) below alert threshold "
            f"{threshold} - no alert"
        )

    state["last_huginn_timeout_count"] = huginn_timeout_count
    return False


def format_epoch_boundary_eta(boundary: float) -> str:
    """Render a boundary as "in about 4h 14m, around 21:34 local (18:34 UTC)".

    Clock times follow the container's TZ env, which defaults to UTC. UTC is
    named on its own when the two coincide, and alongside the local time
    otherwise, so the estimate never depends on the reader knowing how the
    monitor happens to be configured.
    """
    remaining = max(0, int(boundary - time.time()))
    hours, minutes = divmod(remaining // 60, 60)
    delta = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"

    local = datetime.fromtimestamp(boundary).astimezone()
    if local.utcoffset() == timedelta(0):
        clock = f"around {local.strftime('%H:%M')} UTC"
    else:
        utc = datetime.fromtimestamp(boundary, timezone.utc)
        clock = f"around {local.strftime('%H:%M')} local ({utc.strftime('%H:%M')} UTC)"
    return f"in about {delta}, {clock}"


def warn_if_leaving_next_epoch(
    enabled: bool,
    validator: ValidatorConfig,
    state: Dict[str, Any],
    is_active: Optional[bool],
    validator_set: Optional[ValidatorSetState],
    huginn_client: Optional[HuginnClient],
    alerts: AlertHandler,
    next_epoch_boundary: Optional[float] = None,
) -> bool:
    """Warn once per pending exit when a validator is set to leave the active set.

    The Huginn staking validator-set endpoint is the only forward-looking
    source: it lists the validators that are in the current consensus set but
    not in the next epoch's snapshot. This helper sends a WARNING (Telegram +
    Discord + Slack, never Pushover) when all of the following hold:

    - the feature is enabled in config;
    - the validator set is available;
    - the validator's secp address resolves to an API id;
    - that id is in the leaving list;
    - the combined active-set verdict is True (Huginn ``is_active``,
      overridden by gmonads on disagreement - the same verdict the state
      machine consumes);
    - this pending exit has not been warned about yet.

    The unit of dedup is the pending *exit*, not the epoch label Huginn happens
    to report it under: an exit can span several epochs when a delay period
    pushes the transition, and the label advances while the exit itself does
    not, which used to replay the same warning once per epoch. The marker is
    cleared only on positive evidence that the schedule is gone - the id is no
    longer in the leaving list - so neither a label change nor a verdict that
    flaps back to False around a boundary can re-announce the same exit.

    `next_epoch_boundary` only names the exit time; without it - or while the
    network is in a delay period, where the transition provably slips - the
    message states the boundary without a clock time.

    Fail-open by design: missing/failed Huginn data skips the warning and
    never raises.

    Returns:
        True if a warning was sent, False otherwise.
    """
    if not enabled:
        debug(f"{validator.name}: validator set warning disabled - skipping")
        return False

    network = validator.network or "testnet"
    validator_id = (
        huginn_client.get_validator_id(validator.validator_secp, network)
        if huginn_client
        else None
    )

    # Episode bookkeeping. Only a leaving list that no longer names this
    # validator proves the pending exit is over - the validator left the set,
    # or the exit was called off. Unavailable data leaves the marker alone on
    # purpose, so a transient False verdict around an epoch boundary cannot
    # replay the warning for an exit that is still scheduled.
    if (
        validator_set is not None
        and validator_id is not None
        and not validator_set.is_leaving(validator_id)
    ):
        state["last_validator_set_warning_epoch"] = None

    if is_active is not True:
        if is_active is False:
            debug(f"{validator.name}: not in active set (combined verdict) - skipping next-epoch exit warning")
        else:
            debug(f"{validator.name}: active-set verdict unknown - skipping next-epoch exit warning")
        return False

    if validator_set is None:
        debug(f"{validator.name}: validator set unavailable - skipping next-epoch exit warning")
        return False

    if validator_id is None:
        debug(f"{validator.name}: unresolved validator id on {network} - skipping next-epoch exit warning")
        return False

    if not validator_set.is_leaving(validator_id):
        debug(
            f"{validator.name}: not in leaving list for epoch {validator_set.epoch} "
            f"- no next-epoch exit warning"
        )
        return False

    epoch = validator_set.epoch
    if state.get("last_validator_set_warning_epoch") is not None:
        debug(
            f"{validator.name}: next-epoch exit already announced "
            f"(epoch {state['last_validator_set_warning_epoch']}, now {epoch}) - skipping"
        )
        return False

    stake = next(
        (
            entry.get("stake")
            for entry in validator_set.leaving
            if entry.get("validator_id") == validator_id
        ),
        None,
    )

    # The boundary is only named when it can be trusted: a delay period pushes
    # the transition by definition, and there the estimate would point at the
    # wrong boundary. Absent/unusable cadence data just drops the clock time.
    eta = ""
    if next_epoch_boundary is not None and not validator_set.in_delay_period:
        eta = f" - {format_epoch_boundary_eta(next_epoch_boundary)}"

    message = (
        f"*{escape_markdown(validator.name)}*\n\n"
        f"⚠️ Leaving Active Set Next Epoch\n\n"
        f"You are in the active set right now. On-chain staking data has "
        f"validator id {validator_id} in the leaving list (stake {stake}), so "
        f"the exit takes effect at the next epoch boundary{eta}."
    )
    if validator_set.in_delay_period:
        message += (
            "\n\nThe network is in a delay period; the transition may slip"
            " one further epoch."
        )
    if (validator.network or "testnet").lower() == "testnet":
        message += (
            "\n\nNote: on testnet, active-set membership is rotated in batches by"
            " an automated rotation script; a leaving entry is often routine and"
            " typically reverses in a later epoch."
        )

    alert_success = alerts.alert_warning(message)
    if not alert_success:
        error(f"Failed to send next-epoch exit warning for {validator.name}")
        return False

    state["last_validator_set_warning_epoch"] = epoch
    info(
        f"⚠️ {validator.name}: leaving active set next epoch "
        f"(validator id {validator_id}, epoch {epoch}) - warning sent"
    )
    return True


def notify_if_entering_next_epoch(
    enabled: bool,
    validator: ValidatorConfig,
    state: Dict[str, Any],
    is_active: Optional[bool],
    validator_set: Optional[ValidatorSetState],
    huginn_client: Optional[HuginnClient],
    alerts: AlertHandler,
    next_epoch_boundary: Optional[float] = None,
) -> bool:
    """Announce once per pending re-entry when the validator is queued back in.

    Mirror of warn_if_leaving_next_epoch: the same staking validator-set names
    the validators that are in the next epoch's snapshot but not in the current
    consensus set, i.e. who is about to join. The pair brackets the lifecycle
    the operator sees:

        leaving next epoch  -> LEFT ACTIVE SET
        entering next epoch -> RE-ENTERED ACTIVE SET

    Sent as INFO (Telegram + Discord + Slack): a scheduled return is good news
    and nothing is failing, the operator only has to be ready at the boundary.
    Dedup works exactly like the exit notice - one announcement per pending
    re-entry, cleared only once the id has left the entering list, so neither a
    label change nor a flapping verdict can replay it.

    The gate requires a definite inactive verdict: an unknown one (None) would
    risk telling a validator that is in the active set that it is not.

    Fail-open by design: missing/failed Huginn data skips the notice and never
    raises.

    Returns:
        True if a notice was sent, False otherwise.
    """
    if not enabled:
        debug(f"{validator.name}: re-entry notice disabled - skipping")
        return False

    network = validator.network or "testnet"
    validator_id = (
        huginn_client.get_validator_id(validator.validator_secp, network)
        if huginn_client
        else None
    )

    # Episode bookkeeping, same rule as the exit notice: the marker is only
    # cleared when the entering list proves the pending return is over.
    if (
        validator_set is not None
        and validator_id is not None
        and not validator_set.is_entering(validator_id)
    ):
        state["last_validator_set_entry_notice_epoch"] = None

    if is_active is not False:
        if is_active is True:
            debug(
                f"{validator.name}: still in the active set (combined verdict) "
                f"- skipping re-entry notice"
            )
        else:
            debug(f"{validator.name}: active-set verdict unknown - skipping re-entry notice")
        return False

    if validator_set is None:
        debug(f"{validator.name}: validator set unavailable - skipping re-entry notice")
        return False

    if validator_id is None:
        debug(f"{validator.name}: unresolved validator id on {network} - skipping re-entry notice")
        return False

    if not validator_set.is_entering(validator_id):
        debug(
            f"{validator.name}: not in entering list for epoch {validator_set.epoch} "
            f"- no re-entry notice"
        )
        return False

    epoch = validator_set.epoch
    if state.get("last_validator_set_entry_notice_epoch") is not None:
        debug(
            f"{validator.name}: re-entry already announced "
            f"(epoch {state['last_validator_set_entry_notice_epoch']}, now {epoch}) - skipping"
        )
        return False

    # Same rule as the exit notice: no clock time while a delay period can push
    # the transition, and none without usable cadence data.
    eta = ""
    if next_epoch_boundary is not None and not validator_set.in_delay_period:
        eta = f" - {format_epoch_boundary_eta(next_epoch_boundary)}"

    message = (
        f"*{escape_markdown(validator.name)}*\n\n"
        f"🟢 Entering the Active Set Next Epoch\n\n"
        f"You are not in the active set right now. On-chain staking data has "
        f"validator id {validator_id} in the entering list, so the re-entry "
        f"takes effect at the next epoch boundary{eta}."
    )
    if validator_set.in_delay_period:
        message += (
            "\n\nThe network is in a delay period; the transition may slip"
            " one further epoch."
        )

    if not alerts.alert_info(message):
        error(f"Failed to send re-entry notice for {validator.name}")
        return False

    state["last_validator_set_entry_notice_epoch"] = epoch
    info(
        f"🟢 {validator.name}: entering active set next epoch "
        f"(validator id {validator_id}, epoch {epoch}) - notice sent"
    )
    return True


def entering_reentry_note(
    validator_id: Optional[int],
    validator_set: Optional[ValidatorSetState],
) -> str:
    """Return a re-entry note for a LEFT alert when the validator is queued back in.

    The Huginn staking validator-set entering list is the forward-looking source
    for returns: a validator that just left the active set but appears there is
    scheduled to come back at the next epoch boundary (routine on testnet, where
    an automated rotation script cycles validators). Missing data returns "" so
    the LEFT alert is sent unchanged.
    """
    if validator_set is None or validator_id is None:
        return ""
    if not validator_set.is_entering(validator_id):
        return ""
    return (
        "\n\nℹ️ The validator is in the entering list for the next epoch"
        f" (epoch {validator_set.epoch}) - expected to return at the next"
        " epoch boundary."
    )


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
    health_server_port = int(os.getenv("HEALTH_PORT", health_server_config.get("port", 8181)))
    health_server_host = health_server_config.get("host", "0.0.0.0")
    # /health answers "is the monitor loop still ticking?" and turns a heartbeat
    # older than this into a 503. Bounded by the per-check timeouts, so it is
    # independent of the validator count and of check_interval.
    health_staleness_threshold = float(
        health_server_config.get(
            "staleness_threshold", DEFAULT_STALENESS_THRESHOLD_SECONDS
        )
    )

    if health_server_enabled:
        health_server = HealthServer(
            host=health_server_host,
            port=health_server_port,
            staleness_threshold=health_staleness_threshold,
        )
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
    dashboard_server_port = int(os.getenv("DASHBOARD_PORT", dashboard_server_config.get("port", 8282)))
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

    # Initialize API server for monitoring dashboard (optional)
    api_password = os.getenv("DASHBOARD_PASSWORD", "")
    api_jwt_secret = os.getenv("DASHBOARD_JWT_SECRET", "")
    api_port = int(os.getenv("API_PORT", "8383"))
    api_server = None

    if api_password and api_jwt_secret:
        validators_list = []
        for vc in validators:
            validators_list.append({
                "name": vc.name,
                "host": vc.host,
                "network": getattr(vc, "network", "testnet"),
                "metrics_port": getattr(vc, "metrics_port", 8889),
                "node_exporter_port": getattr(vc, "node_exporter_port", None),
            })
        api_server = APIServer(
            prometheus_url="http://prometheus:9090",
            password=api_password,
            jwt_secret=api_jwt_secret,
            validators_config=validators_list,
            port=api_port,
        )
        api_server.start()
        info(f"API server started on 0.0.0.0:{api_port}")
    else:
        info("API server disabled (DASHBOARD_PASSWORD and DASHBOARD_JWT_SECRET not set)")

    # Ensure state directory exists (for Docker volume persistence)
    state_dir = STATE_DIR
    if not os.path.exists(state_dir):
        try:
            os.makedirs(state_dir, exist_ok=True)
            debug(f"Created state directory: {state_dir}")
        except OSError as e:
            warning(f"Failed to create state directory {state_dir}: {e}. Using current directory.")
            state_dir = "."

    # Initialize components
    alerts = AlertHandler(
        telegram_token=config["telegram"]["token"],
        telegram_chat_id=config["telegram"]["chat_id"],
        pushover_user_key=config["pushover"].get("user_key"),
        pushover_app_token=config["pushover"].get("app_token"),
        discord_webhook_url=config.get("discord", {}).get("webhook_url"),
        slack_webhook_url=config.get("slack", {}).get("webhook_url"),
        # Persisted on the state volume so a restart/crash cannot drop a queued
        # CRITICAL; writes are best-effort (a read-only dir degrades to memory)
        failed_alerts_path=os.path.join(state_dir, "failed_alerts.json"),
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
        nvme_wear_warning=thresholds_config.get("nvme_wear_warning", 70),
        nvme_wear_critical=thresholds_config.get("nvme_wear_critical", 95),
    )

    # State tracking for each validator
    states: Dict[str, Dict] = {}
    state_machines: Dict[str, ValidatorStateMachine] = {}
    health_checkers: Dict[str, ValidatorHealthChecker] = {}

    # New-version update checker (checks GHCR weekly and notifies via non-Pushover channels)
    version_checker = None
    updates_config = load_updates_config()
    if updates_config["enabled"]:
        version_checker = VersionChecker(
            image=updates_config["image"],
            check_interval=updates_config["check_interval"],
            state_file=os.path.join(state_dir, "last_notified_version.json"),
            alerts=alerts,
        )
        info(f"Version update check enabled (every {updates_config['check_interval']}s)")
    else:
        info("Version update check disabled")

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
            "ts_fails": 0,  # Consecutive ts_validation_fail increases (separate from main fails)
            "ts_alert_active": False,  # Whether ts_validation_fail alert is currently active
            "last_huginn_timeout_count": None,  # Track Huginn timeout count (network-visible timeouts)
            "last_validator_set_warning_epoch": None,  # Epoch announced for the pending M4 exit (None = none announced)
            "last_validator_set_entry_notice_epoch": None,  # Epoch announced for the pending re-entry (None = none announced)
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

    # Huginn network-visible timeout alert threshold (min missed rounds per check window)
    huginn_timeout_alert_threshold = config["monitoring"].get("huginn_timeout_alert_threshold", 1)

    # Next-epoch active set exit warning (Huginn staking data), on by default
    validator_set_warning_enabled = config["monitoring"].get("validator_set_warning", True)

    # Next-epoch re-entry notice (the mirror of the exit warning), on by default
    validator_set_entry_notice_enabled = config["monitoring"].get(
        "validator_set_entry_notice", True
    )

    # Send startup notification
    health_reporter.send_startup_report(validators)
    info(f"Monitor started - {len(validators)} validators | Log level: {log_level}")

    # Main monitoring loop
    try:
        while running:
            timestamp = datetime.now().strftime("%H:%M:%S")
            all_healthy = True
            loop_tick = time.time()  # refreshed per validator, see below
            health_server_validators: Dict[str, Dict[str, Any]] = {}
            # Huginn staking validator set, fetched at most once per network per iteration
            validator_set_cache: Dict[str, Optional[ValidatorSetState]] = {}
            # Boundary of the epoch each network's set describes (unix ts), same cadence
            epoch_boundary_cache: Dict[str, Optional[float]] = {}

            for validator in validators:
                if not running:
                    break

                state = states[validator.name]
                state_machine = state_machines[validator.name]

                # Get or create health checker (re-use for rate-based CPU calculation)
                if validator.name not in health_checkers:
                    health_checkers[validator.name] = ValidatorHealthChecker(
                        validator=validator,
                        timeout=config["monitoring"].get("timeout", 10),
                        thresholds=thresholds,
                        huginn_client=huginn_client,
                        gmonads_client=gmonads_client,
                    )
                checker = health_checkers[validator.name]

                # Heartbeat: /health reports "stale" once this stops advancing, so a
                # wedged loop becomes visible from outside instead of looking healthy.
                # Taken per validator so it is independent of the validator count.
                loop_tick = time.time()

                try:
                    # Perform health check
                    health_status, current_commits, current_execution_lagging, current_ts_validation_fail, ts_fail_increasing = checker.check(
                        state["last_commits"],
                        state.get("last_execution_lagging"),
                        state.get("last_ts_validation_fail"),
                    )
                    state["last_commits"] = current_commits
                    state["last_execution_lagging"] = current_execution_lagging
                    state["last_ts_validation_fail"] = current_ts_validation_fail

                    # Huginn staking validator set, fetched at most once per
                    # VALIDATOR_SET_CACHE_TTL per network (client-side cache).
                    # Needed both for the next-epoch exit warning and for the
                    # re-entry note on LEFT alerts.
                    network = validator.network or "testnet"
                    if huginn_client and network not in validator_set_cache:
                        try:
                            validator_set_cache[network] = huginn_client.get_validator_set(network)
                        except Exception as e:
                            debug(f"Validator set fetch failed for {network}: {e}")
                            validator_set_cache[network] = None

                    # Boundary ending the epoch the set above describes, used only
                    # to name the exit time. Computed per network per cycle; the
                    # client caches the cadence for hours, so this is at most one
                    # request per epoch.
                    if huginn_client and network not in epoch_boundary_cache:
                        try:
                            boundary_set = validator_set_cache.get(network)
                            epoch_boundary_cache[network] = huginn_client.get_next_epoch_boundary(
                                network, boundary_set.epoch if boundary_set else None
                            )
                        except Exception as e:
                            debug(f"Next-epoch boundary unavailable for {network}: {e}")
                            epoch_boundary_cache[network] = None
                        else:
                            debug(
                                f"Epoch boundary for {network}: "
                                f"{epoch_boundary_cache[network]}"
                            )

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
                                f"uptime={h.get('uptime_percent')}%, "
                                f"total_events={h.get('total_events')}"
                            )

                        # Update state machine with validator status
                        is_active = health_status.is_active_validator
                        is_ever_active = False
                        if health_status.huginn_data:
                            is_ever_active = health_status.huginn_data.get("is_ever_active", False)

                        # Feed the active-set verdict into the state machine. An
                        # unknown verdict (None) holds the current state instead of
                        # being coerced to "inactive", which used to send a spurious
                        # LEFT alert and mute the active-only alerts until the
                        # sources recovered.
                        transition = apply_active_set_transition(
                            state_machine,
                            validator.name,
                            is_active,
                            is_ever_active,
                        )

                        # Handle state transitions with alerts (Telegram + Discord)
                        if transition and transition.is_significant():
                            alert_msg = transition.get_alert_message()
                            if (
                                transition.from_state == ValidatorState.ACTIVE
                                and transition.to_state == ValidatorState.INACTIVE
                                and huginn_client
                            ):
                                leaving_set = validator_set_cache.get(network)
                                reentry_note = entering_reentry_note(
                                    huginn_client.get_validator_id(
                                        validator.validator_secp, network
                                    ),
                                    leaving_set,
                                )
                                alert_msg += reentry_note
                                if reentry_note:
                                    # This LEFT alert already carries the return,
                                    # so the standalone re-entry notice must not
                                    # repeat it for the same episode.
                                    state["last_validator_set_entry_notice_epoch"] = (
                                        leaving_set.epoch if leaving_set else None
                                    )
                            alerts.alert_info(alert_msg)
                            info(f"State transition for {validator.name}: {transition.from_state.value} -> {transition.to_state.value}")

                        # Check for Huginn timeout_count increase (network-visible timeouts)
                        # This is the REAL timeout that matters - if Huginn sees timeouts, validator missed rounds
                        if health_status.huginn_data:
                            handle_huginn_timeout(
                                state=state,
                                validator_name=validator.name,
                                huginn_data=health_status.huginn_data,
                                threshold=huginn_timeout_alert_threshold,
                                alerts=alerts,
                            )
                        else:
                            # Huginn unavailable - no local_timeout fallback
                            # local_timeout metric tracks OTHER nodes' timeouts, not our validator's status
                            # We rely on gmonads fallback (already implemented) and local health metrics
                            debug(f"Huginn unavailable for {validator.name}, relying on gmonads and local metrics")

                    # M4: warn once per epoch when this validator is set to leave
                    # the active set next epoch, using the validator set fetched
                    # earlier in this iteration.
                    warn_if_leaving_next_epoch(
                        enabled=validator_set_warning_enabled,
                        validator=validator,
                        state=state,
                        is_active=health_status.is_active_validator,
                        validator_set=validator_set_cache.get(network),
                        next_epoch_boundary=epoch_boundary_cache.get(network),
                        huginn_client=huginn_client,
                        alerts=alerts,
                    )

                    # Mirror notice: the same set names who is queued to join,
                    # so a pending return is announced as far ahead as the exit.
                    notify_if_entering_next_epoch(
                        enabled=validator_set_entry_notice_enabled,
                        validator=validator,
                        state=state,
                        is_active=health_status.is_active_validator,
                        validator_set=validator_set_cache.get(network),
                        next_epoch_boundary=epoch_boundary_cache.get(network),
                        huginn_client=huginn_client,
                        alerts=alerts,
                    )

                    # Update health server validator data
                    health_server_validators[validator.name] = {
                        "state": state_machine.current_state.value,
                        "healthy": health_status.is_healthy,
                        "height": state.get("last_height"),
                        "peers": state.get("last_peers"),
                        "fails": state["fails"],
                        "huginn_data": health_status.huginn_data,
                        "last_check": time.time(),  # Timestamp for last check
                        "network": validator.network,  # Per-validator network
                        "system_metrics": None,
                        "block_production": None,
                        "warnings": health_status.warnings or [],
                        "criticals": health_status.criticals or [],
                        "rpc_healthy": health_status.rpc_healthy if health_status.rpc_healthy is not None else True,
                    }

                    # Flatten system metrics into dashboard-friendly format
                    if health_status.system_metrics:
                        sm = health_status.system_metrics
                        triedb_data = sm.get("triedb", {})
                        health_server_validators[validator.name]["system_metrics"] = {
                            "cpu_used_percent": sm.get("cpu_used_percent"),
                            "mem_percent": sm.get("mem_percent"),
                            "disk_percent": sm.get("disk_percent"),
                            "triedb_used_percent": triedb_data.get("used_percent") if isinstance(triedb_data, dict) else None,
                        }

                    # Add block production metrics
                    if health_status.metrics:
                        health_server_validators[validator.name]["block_production"] = {
                            "proposals": health_status.metrics.get("proposals"),
                            "block_commits": health_status.metrics.get("block_commits"),
                            "local_timeout": health_status.metrics.get("local_timeout"),
                        }

                    # Handle warnings (non-critical alerts)
                    if health_status.warnings:
                        for warn_msg in health_status.warnings:
                            # Track warning occurrences
                            warning_key = warn_msg.split(":")[0]  # e.g., "CPU", "Memory", "Disk"
                            state["warning_counts"][warning_key] = state["warning_counts"].get(warning_key, 0) + 1

                            # Send warning alert after 3 consecutive occurrences
                            if state["warning_counts"][warning_key] == 3:
                                alerts.alert_warning(f"*{escape_markdown(validator.name)}*\n\n{warn_msg}")
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
                                    f"*{escape_markdown(validator.name)}*\n\n{critical_msg}",
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

                        # Handle ts_validation_fail tracking (separate from main health)
                        # ts_validation_fail is often network-wide (clock skew), not validator-specific
                        ts_threshold = config["monitoring"].get("ts_validation_fail_threshold", 10)
                        if ts_fail_increasing:
                            state["ts_fails"] += 1
                            warning(f"⚠️ {validator.name}: Timestamp validation fails increasing ({state['ts_fails']}/{ts_threshold})")

                            if state["ts_fails"] >= ts_threshold and not state["ts_alert_active"]:
                                # Send WARNING (not CRITICAL) for ts_validation_fail
                                alert_success = alerts.alert_warning(
                                    f"*{escape_markdown(validator.name)}*\n\n⚠️ Persistent timestamp validation fails detected\n"
                                    f"This may be a network-wide issue (clock skew/NTP)\n\n"
                                    f"{health_status.message}"
                                )
                                if alert_success:
                                    state["ts_alert_active"] = True
                        else:
                            if state["ts_fails"] > 0:
                                state["ts_fails"] = 0
                            # Recovery notification for ts_validation_fail
                            if state["ts_alert_active"]:
                                alerts.alert_info(
                                    f"✅ *{escape_markdown(validator.name)}*\n\nTimestamp validation fails stabilized"
                                )
                                state["ts_alert_active"] = False

                        # Recovery notification (Telegram + Discord)
                        if state["alert_active"]:
                            recovery_msg = f"✅ *{escape_markdown(validator.name)} RECOVERED*\n\n{health_status.message}"
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
                                f"Huginn [{validator.name}]: "
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
                                f"*{escape_markdown(validator.name)}*\n\n{health_status.message}",
                                validator_name=validator.name,
                            )
                            # Only mark alert_active if alert was actually sent
                            # This prevents missing alerts due to send failures
                            if alert_success:
                                state["alert_active"] = True
                            else:
                                error(f"Failed to send CRITICAL alert for {validator.name} - will retry next cycle")

                except Exception as exc:
                    # One validator's failure must not abort the cycle for the
                    # others (and never the monitor): log it, mark this validator
                    # unhealthy for this cycle and keep going.
                    all_healthy = False
                    handle_validator_failure(
                        validator,
                        state,
                        health_server_validators,
                        exc,
                        state_machine.current_state.value,
                    )

                # Brief pause between validator checks
                time.sleep(1)

            # Contain a failure in the per-cycle steps (publishing, reports,
            # version check, retry queue): a bad cycle must not stop monitoring.
            try:
                # The loop is still making progress: refresh the /health
                # heartbeat before the tail steps (report + bounded retry batch)
                if health_server:
                    health_server.touch_heartbeat()
                # Fetch per-network TPS from gmonads and attach to each validator.
                # Kept BEFORE publishing: the HTTP threads serialize these dicts, so
                # the loop must finish writing them before they become visible.
                if gmonads_client:
                    networks_seen = {v.network for v in validators if v.network}
                    network_tps = {}
                    network_tps_fetched_at = {}
                    for net in networks_seen:
                        try:
                            block_metrics = gmonads_client.get_block_metrics_1m(network=net)
                            if block_metrics is not None:
                                network_tps[net] = block_metrics.avg_tps
                                # The client serves its cache when the API errors, so
                                # the age of this timestamp is the gmonads
                                # reachability signal shown in the card footer.
                                network_tps_fetched_at[net] = block_metrics.fetched_at
                        except Exception as e:
                            debug(f"Failed to fetch TPS for {net}: {e}")
                    # Attach TPS to each validator based on their network
                    for vname, vdata in health_server_validators.items():
                        vdata["network_tps"] = network_tps.get(vdata.get("network"))
                        vdata["network_tps_fetched_at"] = network_tps_fetched_at.get(vdata.get("network"))

                # Update health server with the overall status, the loop heartbeat and
                # the per-channel alert delivery counters. /health derives its HTTP
                # status from the heartbeat (503 = loop stopped advancing) and keeps
                # the validator aggregate in the body, so an external watchdog can be
                # pointed at it without a validator problem looking like a dead monitor.
                if health_server:
                    health_server.update_status(
                        is_healthy=all_healthy,
                        validators=health_server_validators,
                        loop_tick=loop_tick,
                        alerts=alerts.get_channel_stats(),
                    )

                # Update dashboard server with validator data
                if dashboard_server:
                    health_status_obj = health_server.get_health_status() if health_server else None
                    dashboard_server.update_validators(
                        validators=health_server_validators,
                        status="healthy" if all_healthy else "unhealthy",
                        uptime_seconds=health_status_obj.uptime_seconds if health_status_obj else 0.0,
                        loop_tick=loop_tick,
                    )

                # Check if it's time for extended health report (6-hour detailed report)
                health_reporter.maybe_send_extended_report(validators, states, metrics_data)

                # Check for new monitor releases (weekly, silent if unreachable)
                if version_checker:
                    version_checker.maybe_notify()

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

            except Exception as exc:
                error(f"Monitor cycle failed unexpectedly: {exc}")
                debug(traceback.format_exc())

            # Wait for next cycle with interruptible sleep
            # This allows quick response to SIGTERM by checking running flag every second
            if running:
                sleep_interval = config["monitoring"].get("check_interval", 60)
                slept = 0
                while running and slept < sleep_interval:
                    time.sleep(1)
                    slept += 1
                    # Keep /health fresh while waiting: the wait is part of a
                    # progressing loop, not a stall - otherwise a large
                    # check_interval would look like a wedge
                    if health_server:
                        health_server.touch_heartbeat()

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

        if api_server:
            info("Stopping API server...")
            api_server.stop()

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
