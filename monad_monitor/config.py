"""Configuration loader with environment variable support and validation"""

import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional

import yaml

from monad_monitor.huginn import HuginnConfig
from monad_monitor.gmonads import GmonadsConfig


class ConfigValidationError(Exception):
    """Raised when configuration validation fails"""
    pass




@dataclass
class ValidatorConfig:
    """Validator configuration"""
    name: str
    host: str
    metrics_port: int
    rpc_port: int
    node_exporter_port: Optional[int]
    validator_secp: str
    enabled: bool
    network: str = "testnet"  # Network this validator runs on (testnet or mainnet)

    @property
    def metrics_url(self) -> str:
        return f"http://{self.host}:{self.metrics_port}/metrics"

    @property
    def rpc_url(self) -> str:
        return f"http://{self.host}:{self.rpc_port}"

    @property
    def node_exporter_url(self) -> Optional[str]:
        if self.node_exporter_port:
            return f"http://{self.host}:{self.node_exporter_port}/metrics"
        return None


def load_config() -> Dict[str, Any]:
    """Load main configuration file with environment variable substitution"""
    config_path = os.getenv("CONFIG_PATH", "config/config.yaml")

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Environment variable substitution for sensitive values
    config["telegram"]["token"] = os.getenv(
        "TELEGRAM_TOKEN", config["telegram"].get("token", "")
    )
    config["telegram"]["chat_id"] = os.getenv(
        "TELEGRAM_CHAT_ID", config["telegram"].get("chat_id", "")
    )
    config["pushover"]["user_key"] = os.getenv(
        "PUSHOVER_USER_KEY", config["pushover"].get("user_key", "")
    )
    config["pushover"]["app_token"] = os.getenv(
        "PUSHOVER_APP_TOKEN", config["pushover"].get("app_token", "")
    )

    # Discord webhook (optional)
    if "discord" not in config:
        config["discord"] = {}
    config["discord"]["webhook_url"] = os.getenv(
        "DISCORD_WEBHOOK_URL", config["discord"].get("webhook_url", "")
    )

    return config


def validate_config(config: Dict[str, Any]) -> None:
    """
    Validate configuration at startup.

    Raises ConfigValidationError if critical configuration is missing.
    """
    errors = []

    # Check Telegram configuration
    telegram = config.get("telegram", {})
    if not telegram.get("token"):
        errors.append("Telegram token is missing (set TELEGRAM_TOKEN env var or config.yaml)")
    if not telegram.get("chat_id"):
        errors.append("Telegram chat_id is missing (set TELEGRAM_CHAT_ID env var or config.yaml)")

    # Check monitoring configuration
    monitoring = config.get("monitoring", {})
    check_interval = monitoring.get("check_interval", 60)
    if check_interval < 10:
        errors.append(f"check_interval ({check_interval}s) is too low - minimum is 10 seconds")
    if check_interval > 3600:
        errors.append(f"check_interval ({check_interval}s) is too high - maximum is 3600 seconds")

    # Check thresholds
    thresholds = config.get("thresholds", {})
    cpu_warning = thresholds.get("cpu_warning", 90)
    cpu_critical = thresholds.get("cpu_critical", 95)
    if cpu_warning >= cpu_critical:
        errors.append(f"cpu_warning ({cpu_warning}) must be less than cpu_critical ({cpu_critical})")

    mem_warning = thresholds.get("memory_warning", 90)
    mem_critical = thresholds.get("memory_critical", 95)
    if mem_warning >= mem_critical:
        errors.append(f"memory_warning ({mem_warning}) must be less than memory_critical ({mem_critical})")

    disk_warning = thresholds.get("disk_warning", 85)
    disk_critical = thresholds.get("disk_critical", 95)
    if disk_warning >= disk_critical:
        errors.append(f"disk_warning ({disk_warning}) must be less than disk_critical ({disk_critical})")

    if errors:
        error_msg = "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ConfigValidationError(error_msg)


def load_validators() -> List[ValidatorConfig]:
    """Load validator list from configuration file"""
    validators_path = os.getenv("VALIDATORS_PATH", "config/validators.yaml")

    with open(validators_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    validators = []
    for v in data.get("validators", []):
        if v.get("enabled", True):
            validators.append(
                ValidatorConfig(
                    name=v["name"],
                    host=v["host"],
                    metrics_port=v.get("metrics_port", 8889),
                    rpc_port=v.get("rpc_port", 8080),
                    node_exporter_port=v.get("node_exporter_port"),
                    validator_secp=v.get("validator_secp", ""),
                    enabled=v.get("enabled", True),
                    network=v.get("network", "testnet"),  # Default to testnet
                )
            )

    return validators


def validate_validators(validators: List[ValidatorConfig]) -> None:
    """
    Validate validator configuration at startup.

    Raises ConfigValidationError if critical configuration is missing.
    """
    errors = []

    if not validators:
        errors.append("No validators configured - add at least one validator to validators.yaml")

    for v in validators:
        if not v.name:
            errors.append("Validator missing 'name' field")
        if not v.host:
            errors.append(f"Validator '{v.name or 'unknown'}' missing 'host' field")
        if not v.validator_secp:
            errors.append(f"Validator '{v.name}' missing 'validator_secp' - required for active set detection via Huginn/gmonads APIs")
        if v.metrics_port and (v.metrics_port < 1 or v.metrics_port > 65535):
            errors.append(f"Validator '{v.name}' has invalid metrics_port: {v.metrics_port}")
        if v.rpc_port and (v.rpc_port < 1 or v.rpc_port > 65535):
            errors.append(f"Validator '{v.name}' has invalid rpc_port: {v.rpc_port}")

    if errors:
        error_msg = "Validator configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ConfigValidationError(error_msg)


def load_huginn_config() -> HuginnConfig:
    """
    Load Huginn API configuration from main config file.

    Supports both new multi-endpoint format and legacy single base_url format.

    New format (recommended):
        huginn:
          enabled: true
          endpoints:
            testnet: "https://validator-api-testnet.huginn.tech/monad-api"
            mainnet: "https://validator-api.huginn.tech/monad-api"
          check_interval: 3600
          timeout: 10

    Legacy format (backward compatible):
        huginn:
          enabled: true
          base_url: "https://validator-api-testnet.huginn.tech/monad-api"
          check_interval: 3600
          timeout: 10
    """
    config = load_config()
    huginn = config.get("huginn", {})

    # Check for new endpoints format
    endpoints = huginn.get("endpoints")

    if endpoints:
        # New multi-network format
        return HuginnConfig(
            endpoints=endpoints,
            enabled=huginn.get("enabled", True),
            check_interval=huginn.get("check_interval", 3600),
            timeout=huginn.get("timeout", 10),
        )
    else:
        # Legacy single URL format (backward compatible)
        return HuginnConfig(
            base_url=huginn.get(
                "base_url", "https://validator-api-testnet.huginn.tech/monad-api"
            ),
            enabled=huginn.get("enabled", True),
            check_interval=huginn.get("check_interval", 3600),
            timeout=huginn.get("timeout", 10),
        )


def load_gmonads_config() -> GmonadsConfig:
    """
    Load gmonads API configuration from main config file.

    Config format:
        gmonads:
          enabled: true
          base_url: "https://www.gmonads.com/api/v1/public"
          check_interval: 120
          timeout: 10
    """
    config = load_config()
    gmonads = config.get("gmonads", {})

    return GmonadsConfig(
        base_url=gmonads.get("base_url", "https://www.gmonads.com/api/v1/public"),
        enabled=gmonads.get("enabled", True),
        check_interval=gmonads.get("check_interval", 120),
        timeout=gmonads.get("timeout", 10),
    )


