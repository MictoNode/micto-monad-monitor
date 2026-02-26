"""Tests for configuration loading"""

import os
from unittest.mock import patch, mock_open

import pytest

from monad_monitor.config import (
    ValidatorConfig,
    load_validators,
    validate_config,
    validate_validators,
    ConfigValidationError,
)


class TestValidatorConfig:
    """Test cases for ValidatorConfig dataclass"""

    def test_validator_config_creation(self):
        """Test basic ValidatorConfig creation"""
        config = ValidatorConfig(
            name="test-validator",
            host="192.168.1.100",
            metrics_port=8889,
            rpc_port=8080,
            node_exporter_port=9100,
            validator_secp="0x1234567890abcdef",
            enabled=True,
        )

        assert config.name == "test-validator"
        assert config.host == "192.168.1.100"
        assert config.metrics_port == 8889
        assert config.rpc_port == 8080
        assert config.node_exporter_port == 9100
        assert config.validator_secp == "0x1234567890abcdef"
        assert config.enabled is True

    def test_validator_config_defaults(self):
        """Test ValidatorConfig with minimal parameters"""
        config = ValidatorConfig(
            name="minimal",
            host="10.0.0.1",
            metrics_port=8889,
            rpc_port=8080,
            node_exporter_port=None,
            validator_secp="",
            enabled=True,
        )

        assert config.node_exporter_port is None
        assert config.validator_secp == ""

    def test_metrics_url_format(self):
        """Test metrics_url property generates correct format"""
        config = ValidatorConfig(
            name="test",
            host="localhost",
            metrics_port=9000,
            rpc_port=8080,
            node_exporter_port=None,
            validator_secp="",
            enabled=True,
        )

        assert config.metrics_url == "http://localhost:9000/metrics"

    def test_rpc_url_format(self):
        """Test rpc_url property generates correct format"""
        config = ValidatorConfig(
            name="test",
            host="127.0.0.1",
            metrics_port=8889,
            rpc_port=8545,
            node_exporter_port=None,
            validator_secp="",
            enabled=True,
        )

        assert config.rpc_url == "http://127.0.0.1:8545"

    def test_node_exporter_url_when_set(self):
        """Test node_exporter_url when port is set"""
        config = ValidatorConfig(
            name="test",
            host="192.168.1.50",
            metrics_port=8889,
            rpc_port=8080,
            node_exporter_port=9100,
            validator_secp="",
            enabled=True,
        )

        assert config.node_exporter_url == "http://192.168.1.50:9100/metrics"

    def test_node_exporter_url_when_not_set(self):
        """Test node_exporter_url returns None when port not set"""
        config = ValidatorConfig(
            name="test",
            host="192.168.1.50",
            metrics_port=8889,
            rpc_port=8080,
            node_exporter_port=None,
            validator_secp="",
            enabled=True,
        )

        assert config.node_exporter_url is None


class TestLoadValidators:
    """Test cases for load_validators function"""

    VALIDATORS_YAML = """
validators:
  - name: validator-1
    host: 192.168.1.100
    metrics_port: 8889
    rpc_port: 8080
    node_exporter_port: 9100
    validator_secp: "0x1234"
    enabled: true

  - name: validator-2
    host: 192.168.1.101
    metrics_port: 8889
    rpc_port: 8080
    enabled: true

  - name: validator-disabled
    host: 192.168.1.102
    metrics_port: 8889
    rpc_port: 8080
    enabled: false
"""

    def test_load_validators_returns_list(self):
        """Test load_validators returns a list"""
        with patch("builtins.open", mock_open(read_data=self.VALIDATORS_YAML)):
            with patch.dict(os.environ, {"VALIDATORS_PATH": "test.yaml"}):
                result = load_validators()

        assert isinstance(result, list)

    def test_load_validators_filters_disabled(self):
        """Test load_validators filters out disabled validators"""
        with patch("builtins.open", mock_open(read_data=self.VALIDATORS_YAML)):
            with patch.dict(os.environ, {"VALIDATORS_PATH": "test.yaml"}):
                result = load_validators()

        # Only validator-1 and validator-2 should be loaded
        assert len(result) == 2
        names = [v.name for v in result]
        assert "validator-1" in names
        assert "validator-2" in names
        assert "validator-disabled" not in names

    def test_load_validators_uses_defaults(self):
        """Test load_validators uses default values for missing fields"""
        yaml_content = """
validators:
  - name: validator-defaults
    host: 10.0.0.1
"""

        with patch("builtins.open", mock_open(read_data=yaml_content)):
            with patch.dict(os.environ, {"VALIDATORS_PATH": "test.yaml"}):
                result = load_validators()

        assert len(result) == 1
        validator = result[0]
        assert validator.metrics_port == 8889  # Default
        assert validator.rpc_port == 8080  # Default
        assert validator.node_exporter_port is None  # Default
        assert validator.validator_secp == ""  # Default
        assert validator.enabled is True  # Default


class TestLoadConfig:
    """Test cases for load_config function"""

    CONFIG_YAML = """
telegram:
  token: "default-token"
  chat_id: "default-chat"

pushover:
  user_key: "default-user"
  app_token: "default-app"

monitoring:
  check_interval: 60
  alert_threshold: 3
  health_report_interval: 3600
  timeout: 10

thresholds:
  cpu_warning: 90
  cpu_critical: 95
"""

    def test_load_config_env_substitution(self):
        """Test load_config substitutes environment variables"""
        with patch("builtins.open", mock_open(read_data=self.CONFIG_YAML)):
            with patch.dict(
                os.environ,
                {
                    "CONFIG_PATH": "test.yaml",
                    "TELEGRAM_TOKEN": "env-token",
                    "TELEGRAM_CHAT_ID": "env-chat",
                },
            ):
                from monad_monitor.config import load_config

                result = load_config()

        assert result["telegram"]["token"] == "env-token"
        assert result["telegram"]["chat_id"] == "env-chat"


class TestValidateConfig:
    """Test cases for validate_config function"""

    def test_validate_config_missing_telegram_token(self):
        """Test validation fails when Telegram token is missing"""
        config = {
            "telegram": {"chat_id": "test"},
            "monitoring": {},
            "thresholds": {},
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "Telegram token" in str(exc_info.value)

    def test_validate_config_check_interval_too_low(self):
        """Test validation fails when check_interval is too low"""
        config = {
            "telegram": {"token": "test", "chat_id": "test"},
            "monitoring": {"check_interval": 5},
            "thresholds": {},
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "check_interval" in str(exc_info.value)

    def test_validate_config_check_interval_too_high(self):
        """Test validation fails when check_interval is too high"""
        config = {
            "telegram": {"token": "test", "chat_id": "test"},
            "monitoring": {"check_interval": 5000},
            "thresholds": {},
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "check_interval" in str(exc_info.value)

    def test_validate_config_cpu_thresholds_invalid(self):
        """Test validation fails when cpu_warning >= cpu_critical"""
        config = {
            "telegram": {"token": "test", "chat_id": "test"},
            "monitoring": {},
            "thresholds": {"cpu_warning": 95, "cpu_critical": 90},
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "cpu_warning" in str(exc_info.value)

    def test_validate_config_memory_thresholds_invalid(self):
        """Test validation fails when memory_warning >= memory_critical"""
        config = {
            "telegram": {"token": "test", "chat_id": "test"},
            "monitoring": {},
            "thresholds": {"memory_warning": 95, "memory_critical": 90},
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "memory_warning" in str(exc_info.value)

    def test_validate_config_disk_thresholds_invalid(self):
        """Test validation fails when disk_warning >= disk_critical"""
        config = {
            "telegram": {"token": "test", "chat_id": "test"},
            "monitoring": {},
            "thresholds": {"disk_warning": 95, "disk_critical": 90},
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(config)
        assert "disk_warning" in str(exc_info.value)

    def test_validate_config_valid(self):
        """Test validation passes with valid config"""
        config = {
            "telegram": {"token": "test", "chat_id": "test"},
            "monitoring": {"check_interval": 60},
            "thresholds": {"cpu_warning": 90, "cpu_critical": 95},
        }
        # Should not raise
        validate_config(config)


class TestValidateValidators:
    """Test cases for validate_validators function"""

    def test_validate_validators_empty_list(self):
        """Test validation fails when no validators configured"""
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_validators([])
        assert "No validators" in str(exc_info.value)

    def test_validate_validators_missing_name(self):
        """Test validation fails when validator missing name"""
        validators = [
            ValidatorConfig(
                name="",  # Empty name
                host="192.168.1.1",
                metrics_port=8889,
                rpc_port=8080,
                node_exporter_port=None,
                validator_secp="",
                enabled=True,
            )
        ]
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_validators(validators)
        assert "name" in str(exc_info.value)

    def test_validate_validators_missing_host(self):
        """Test validation fails when validator missing host"""
        validators = [
            ValidatorConfig(
                name="test",
                host="",  # Empty host
                metrics_port=8889,
                rpc_port=8080,
                node_exporter_port=None,
                validator_secp="",
                enabled=True,
            )
        ]
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_validators(validators)
        assert "host" in str(exc_info.value)

    def test_validate_validators_invalid_port(self):
        """Test validation fails when port is out of range"""
        validators = [
            ValidatorConfig(
                name="test",
                host="192.168.1.1",
                metrics_port=70000,  # Invalid port
                rpc_port=8080,
                node_exporter_port=None,
                validator_secp="",
                enabled=True,
            )
        ]
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_validators(validators)
        assert "metrics_port" in str(exc_info.value)

    def test_validate_validators_missing_secp(self):
        """Test validation fails when validator_secp is missing"""
        validators = [
            ValidatorConfig(
                name="test",
                host="192.168.1.1",
                metrics_port=8889,
                rpc_port=8080,
                node_exporter_port=None,
                validator_secp="",  # Empty - should fail
                enabled=True,
            )
        ]
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_validators(validators)
        assert "validator_secp" in str(exc_info.value)

    def test_validate_validators_valid(self):
        """Test validation passes with valid validators"""
        validators = [
            ValidatorConfig(
                name="test",
                host="192.168.1.1",
                metrics_port=8889,
                rpc_port=8080,
                node_exporter_port=None,
                validator_secp="02abc123def456789abc123def456789abc123def456789abc123def456789abc123def",
                enabled=True,
            )
        ]
        # Should not raise
        validate_validators(validators)
