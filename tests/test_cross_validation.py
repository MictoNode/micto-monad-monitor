"""Tests for cross-validation module"""

import pytest
from unittest.mock import Mock, patch

from monad_monitor.cross_validation import CrossValidator, CrossValidationResult
from monad_monitor.config import ValidatorConfig


class TestCrossValidationResult:
    """Test cases for CrossValidationResult dataclass"""

    def test_to_dict(self):
        """Should serialize to dictionary correctly"""
        result = CrossValidationResult(
            validator_secp="0x1234",
            huginn_is_active=True,
            gmonads_is_active=True,
            sources_agree=True,
            confidence="high",
            recommended_status=True,
        )

        d = result.to_dict()

        assert d["validator_secp"] == "0x1234"
        assert d["huginn_is_active"] is True
        assert d["gmonads_is_active"] is True
        assert d["sources_agree"] is True
        assert d["confidence"] == "high"
        assert d["recommended_status"] is True


class TestCrossValidator:
    """Test cases for CrossValidator class"""

    @pytest.fixture
    def mock_huginn(self):
        """Create mock HuginnClient"""
        return Mock()

    @pytest.fixture
    def mock_gmonads(self):
        """Create mock GmonadsClient"""
        return Mock()

    @pytest.fixture
    def cross_validator(self, mock_huginn, mock_gmonads):
        """Create CrossValidator with mock clients"""
        return CrossValidator(mock_huginn, mock_gmonads)

    def test_both_sources_agree_active(self, cross_validator, mock_huginn, mock_gmonads):
        """High confidence when both sources agree validator is active"""
        mock_huginn.is_validator_active.return_value = True
        mock_gmonads.is_validator_in_active_set.return_value = True

        result = cross_validator.validate_validator_status("0x1234", "testnet")

        assert result.huginn_is_active is True
        assert result.gmonads_is_active is True
        assert result.sources_agree is True
        assert result.confidence == "high"
        assert result.recommended_status is True

    def test_both_sources_agree_inactive(self, cross_validator, mock_huginn, mock_gmonads):
        """High confidence when both sources agree validator is inactive"""
        mock_huginn.is_validator_active.return_value = False
        mock_gmonads.is_validator_in_active_set.return_value = False

        result = cross_validator.validate_validator_status("0x1234", "testnet")

        assert result.huginn_is_active is False
        assert result.gmonads_is_active is False
        assert result.sources_agree is True
        assert result.confidence == "high"
        assert result.recommended_status is False

    def test_sources_disagree_huginn_active(self, cross_validator, mock_huginn, mock_gmonads):
        """Low confidence when sources disagree, use Huginn as primary"""
        mock_huginn.is_validator_active.return_value = True
        mock_gmonads.is_validator_in_active_set.return_value = False

        result = cross_validator.validate_validator_status("0x1234", "testnet")

        assert result.huginn_is_active is True
        assert result.gmonads_is_active is False
        assert result.sources_agree is False
        assert result.confidence == "low"
        assert result.recommended_status is True  # Uses Huginn

    def test_sources_disagree_gmonads_active(self, cross_validator, mock_huginn, mock_gmonads):
        """Low confidence when sources disagree, use Huginn as primary"""
        mock_huginn.is_validator_active.return_value = False
        mock_gmonads.is_validator_in_active_set.return_value = True

        result = cross_validator.validate_validator_status("0x1234", "testnet")

        assert result.huginn_is_active is False
        assert result.gmonads_is_active is True
        assert result.sources_agree is False
        assert result.confidence == "low"
        assert result.recommended_status is False  # Uses Huginn

    def test_only_huginn_available(self, cross_validator, mock_huginn, mock_gmonads):
        """Medium confidence when only Huginn available"""
        mock_huginn.is_validator_active.return_value = True
        mock_gmonads.is_validator_in_active_set.return_value = None

        result = cross_validator.validate_validator_status("0x1234", "testnet")

        assert result.huginn_is_active is True
        assert result.gmonads_is_active is None
        assert result.sources_agree is True
        assert result.confidence == "medium"
        assert result.recommended_status is True

    def test_only_gmonads_available(self, cross_validator, mock_huginn, mock_gmonads):
        """Medium confidence when only gmonads available"""
        mock_huginn.is_validator_active.return_value = None
        mock_gmonads.is_validator_in_active_set.return_value = True

        result = cross_validator.validate_validator_status("0x1234", "testnet")

        assert result.huginn_is_active is None
        assert result.gmonads_is_active is True
        assert result.sources_agree is True
        assert result.confidence == "medium"
        assert result.recommended_status is True

    def test_no_sources_available(self, cross_validator, mock_huginn, mock_gmonads):
        """Low confidence when no sources available"""
        mock_huginn.is_validator_active.return_value = None
        mock_gmonads.is_validator_in_active_set.return_value = None

        result = cross_validator.validate_validator_status("0x1234", "testnet")

        assert result.huginn_is_active is None
        assert result.gmonads_is_active is None
        assert result.sources_agree is True
        assert result.confidence == "low"
        assert result.recommended_status is False  # Default to inactive

    def test_huginn_exception_handled(self, cross_validator, mock_huginn, mock_gmonads):
        """Should handle Huginn exceptions gracefully"""
        mock_huginn.is_validator_active.side_effect = Exception("API error")
        mock_gmonads.is_validator_in_active_set.return_value = True

        result = cross_validator.validate_validator_status("0x1234", "testnet")

        assert result.huginn_is_active is None
        assert result.gmonads_is_active is True
        assert result.confidence == "medium"

    def test_gmonads_exception_handled(self, cross_validator, mock_huginn, mock_gmonads):
        """Should handle gmonads exceptions gracefully"""
        mock_huginn.is_validator_active.return_value = True
        mock_gmonads.is_validator_in_active_set.side_effect = Exception("API error")

        result = cross_validator.validate_validator_status("0x1234", "testnet")

        assert result.huginn_is_active is True
        assert result.gmonads_is_active is None
        assert result.confidence == "medium"


class TestValidateAllMonitored:
    """Test cases for validate_all_monitored method"""

    @pytest.fixture
    def cross_validator(self):
        """Create CrossValidator with mock clients"""
        mock_huginn = Mock()
        mock_gmonads = Mock()
        return CrossValidator(mock_huginn, mock_gmonads)

    def test_validate_all_monitored(self, cross_validator):
        """Should validate all validators with secp addresses"""
        validators = [
            ValidatorConfig(
                name="validator1",
                host="localhost",
                metrics_port=8889,
                rpc_port=8080,
                node_exporter_port=None,
                validator_secp="0x1111",
                enabled=True,
                network="testnet",
            ),
            ValidatorConfig(
                name="validator2",
                host="localhost",
                metrics_port=8890,
                rpc_port=8081,
                node_exporter_port=None,
                validator_secp="0x2222",
                enabled=True,
                network="testnet",
            ),
            ValidatorConfig(
                name="validator3",
                host="localhost",
                metrics_port=8891,
                rpc_port=8082,
                node_exporter_port=None,
                validator_secp="",  # No secp address
                enabled=True,
                network="testnet",
            ),
        ]

        # Mock responses
        cross_validator.huginn_client.is_validator_active.return_value = True
        cross_validator.gmonads_client.is_validator_in_active_set.return_value = True

        results = cross_validator.validate_all_monitored(validators)

        # Should only validate validators with secp addresses
        assert len(results) == 2
        assert "validator1" in results
        assert "validator2" in results
        assert "validator3" not in results


class TestGetSummary:
    """Test cases for get_summary method"""

    @pytest.fixture
    def cross_validator(self):
        """Create CrossValidator"""
        mock_huginn = Mock()
        mock_gmonads = Mock()
        return CrossValidator(mock_huginn, mock_gmonads)

    def test_get_summary_empty(self, cross_validator):
        """Should handle empty results"""
        summary = cross_validator.get_summary({})

        assert summary["total"] == 0

    def test_get_summary_with_results(self, cross_validator):
        """Should calculate summary statistics correctly"""
        results = {
            "v1": CrossValidationResult(
                validator_secp="0x1",
                huginn_is_active=True,
                gmonads_is_active=True,
                sources_agree=True,
                confidence="high",
                recommended_status=True,
            ),
            "v2": CrossValidationResult(
                validator_secp="0x2",
                huginn_is_active=False,
                gmonads_is_active=False,
                sources_agree=True,
                confidence="high",
                recommended_status=False,
            ),
            "v3": CrossValidationResult(
                validator_secp="0x3",
                huginn_is_active=True,
                gmonads_is_active=None,
                sources_agree=True,
                confidence="medium",
                recommended_status=True,
            ),
            "v4": CrossValidationResult(
                validator_secp="0x4",
                huginn_is_active=True,
                gmonads_is_active=False,
                sources_agree=False,
                confidence="low",
                recommended_status=True,
            ),
        }

        summary = cross_validator.get_summary(results)

        assert summary["total"] == 4
        assert summary["active"] == 3
        assert summary["inactive"] == 1
        assert summary["high_confidence"] == 2
        assert summary["medium_confidence"] == 1
        assert summary["low_confidence"] == 1
        assert summary["sources_agree"] == 3
        assert summary["sources_disagree"] == 1
