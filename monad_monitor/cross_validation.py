"""Cross-validation module for comparing Huginn and gmonads validator status"""

import logging
from dataclasses import dataclass
from typing import Optional, Dict, List

from monad_monitor.huginn import HuginnClient
from monad_monitor.gmonads import GmonadsClient
from monad_monitor.config import ValidatorConfig

logger = logging.getLogger(__name__)


@dataclass
class CrossValidationResult:
    """Result of cross-validating validator status from multiple sources"""
    validator_secp: str
    huginn_is_active: Optional[bool]
    gmonads_is_active: Optional[bool]
    sources_agree: bool
    confidence: str  # "high", "medium", "low"
    recommended_status: bool

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization"""
        return {
            "validator_secp": self.validator_secp,
            "huginn_is_active": self.huginn_is_active,
            "gmonads_is_active": self.gmonads_is_active,
            "sources_agree": self.sources_agree,
            "confidence": self.confidence,
            "recommended_status": self.recommended_status,
        }


class CrossValidator:
    """
    Cross-validator for comparing validator status from multiple sources.

    Strategy:
    - Both sources agree → High confidence, use agreed value
    - One source only → Medium confidence, use available source
    - Sources disagree → Low confidence, use Huginn as primary (more reliable for active set)
    """

    def __init__(self, huginn_client: HuginnClient, gmonads_client: GmonadsClient):
        self.huginn_client = huginn_client
        self.gmonads_client = gmonads_client

    def validate_validator_status(
        self,
        secp_address: str,
        network: str = "testnet"
    ) -> CrossValidationResult:
        """
        Cross-validate validator status from Huginn and gmonads.

        Args:
            secp_address: Validator's secp256k1 public key
            network: Network name ('testnet' or 'mainnet')

        Returns:
            CrossValidationResult with status from both sources and confidence level
        """
        # Get status from Huginn
        huginn_is_active = None
        try:
            huginn_is_active = self.huginn_client.is_validator_active(secp_address, network)
        except Exception as e:
            logger.warning(f"CrossValidator: Huginn error for {secp_address[:16]}...: {e}")

        # Get status from gmonads
        gmonads_is_active = None
        try:
            gmonads_is_active = self.gmonads_client.is_validator_in_active_set(secp_address, network)
        except Exception as e:
            logger.warning(f"CrossValidator: gmonads error for {secp_address[:16]}...: {e}")

        # Determine confidence and agreement
        confidence, sources_agree, recommended_status = self._evaluate_sources(
            huginn_is_active, gmonads_is_active
        )

        return CrossValidationResult(
            validator_secp=secp_address,
            huginn_is_active=huginn_is_active,
            gmonads_is_active=gmonads_is_active,
            sources_agree=sources_agree,
            confidence=confidence,
            recommended_status=recommended_status,
        )

    def _evaluate_sources(
        self,
        huginn_is_active: Optional[bool],
        gmonads_is_active: Optional[bool]
    ) -> tuple:
        """
        Evaluate sources and determine confidence level.

        Args:
            huginn_is_active: Status from Huginn (True/False/None)
            gmonads_is_active: Status from gmonads (True/False/None)

        Returns:
            Tuple of (confidence, sources_agree, recommended_status)
        """
        # Both sources unavailable
        if huginn_is_active is None and gmonads_is_active is None:
            return "low", True, False  # Default to inactive with low confidence

        # Only Huginn available
        if huginn_is_active is not None and gmonads_is_active is None:
            return "medium", True, huginn_is_active

        # Only gmonads available
        if huginn_is_active is None and gmonads_is_active is not None:
            return "medium", True, gmonads_is_active

        # Both sources available
        if huginn_is_active == gmonads_is_active:
            # Sources agree - high confidence
            return "high", True, huginn_is_active
        else:
            # Sources disagree - low confidence, use Huginn as primary
            logger.warning(f"CrossValidator: Sources disagree - Huginn={huginn_is_active}, gmonads={gmonads_is_active}")
            return "low", False, huginn_is_active

    def validate_all_monitored(
        self,
        validators: List[ValidatorConfig]
    ) -> Dict[str, CrossValidationResult]:
        """
        Cross-validate all monitored validators.

        Args:
            validators: List of ValidatorConfig objects

        Returns:
            Dictionary mapping validator name to CrossValidationResult
        """
        results = {}

        for validator in validators:
            if not validator.validator_secp:
                continue

            result = self.validate_validator_status(
                validator.validator_secp,
                validator.network
            )
            results[validator.name] = result

        return results

    def get_summary(self, results: Dict[str, CrossValidationResult]) -> Dict:
        """
        Get summary statistics from cross-validation results.

        Args:
            results: Dictionary of cross-validation results

        Returns:
            Dictionary with summary statistics
        """
        total = len(results)
        if total == 0:
            return {"total": 0}

        high_confidence = sum(1 for r in results.values() if r.confidence == "high")
        medium_confidence = sum(1 for r in results.values() if r.confidence == "medium")
        low_confidence = sum(1 for r in results.values() if r.confidence == "low")

        sources_agree = sum(1 for r in results.values() if r.sources_agree)
        active_count = sum(1 for r in results.values() if r.recommended_status)

        return {
            "total": total,
            "active": active_count,
            "inactive": total - active_count,
            "high_confidence": high_confidence,
            "medium_confidence": medium_confidence,
            "low_confidence": low_confidence,
            "sources_agree": sources_agree,
            "sources_disagree": total - sources_agree,
        }
