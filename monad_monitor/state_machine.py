"""Validator State Machine for tracking validator lifecycle states"""

import json
import logging
import time
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any, List, ClassVar


class ValidatorState(Enum):
    """
    Validator lifecycle states.

    - NEW: Never been in active set (is_ever_active=False)
    - ACTIVE: Currently in active set (is_active=True)
    - INACTIVE: Was active before, now dropped (is_ever_active=True AND is_active=False)
    """
    NEW = "new"
    ACTIVE = "active"
    INACTIVE = "inactive"


@dataclass
class StateTransition:
    """Record of a state transition"""
    from_state: ValidatorState
    to_state: ValidatorState
    validator_name: str
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_significant(self) -> bool:
        """Check if this transition should trigger an alert"""
        # NEW -> ACTIVE: Validator entered active set
        # ACTIVE -> INACTIVE: Validator dropped from active set
        # INACTIVE -> ACTIVE: Validator re-entered active set
        return self.from_state != self.to_state

    def get_alert_message(self) -> str:
        """Generate alert message for this transition"""
        if self.from_state == ValidatorState.NEW and self.to_state == ValidatorState.ACTIVE:
            msg = f"🟢 *{self.validator_name} ENTERED ACTIVE SET*\n\n"
            msg += "Validator is now in the active set and producing blocks!"
            return msg

        elif self.from_state == ValidatorState.ACTIVE and self.to_state == ValidatorState.INACTIVE:
            msg = f"⚪ *{self.validator_name} LEFT ACTIVE SET*\n\n"
            msg += "Validator is no longer in the active set.\n"
            if "round_diff" in self.metadata:
                msg += f"Round difference: {self.metadata['round_diff']}\n"
            msg += "Block production alerts disabled until re-entry."
            return msg

        elif self.from_state == ValidatorState.INACTIVE and self.to_state == ValidatorState.ACTIVE:
            msg = f"🟢 *{self.validator_name} RE-ENTERED ACTIVE SET*\n\n"
            msg += "Validator is back in the active set!\n"
            msg += "Block production alerts re-enabled."
            return msg

        return f"ℹ️ {self.validator_name}: State changed from {self.from_state.value} to {self.to_state.value}"


class ValidatorStateMachine:
    """
    State machine for tracking validator lifecycle.

    State Transitions:
    - NEW -> ACTIVE: When validator first enters active set
    - ACTIVE -> INACTIVE: When validator drops from active set
    - INACTIVE -> ACTIVE: When validator re-enters active set

    Alert Thresholds by State:
    - NEW: Minimal alerts (expected issues during setup)
    - ACTIVE: Full alert sensitivity
    - INACTIVE: Monitor for recovery only
    """

    # Alert types that should always trigger regardless of state
    ALWAYS_ALERT_TYPES = {"node_down", "connection_failed", "rpc_error"}

    # Alert types that should only trigger for ACTIVE validators
    ACTIVE_ONLY_ALERT_TYPES = {"local_timeout", "ts_validation_fail", "execution_lagging"}

    def __init__(self, validator_name: str, initial_state: Optional[ValidatorState] = None):
        self.validator_name = validator_name
        self.current_state = initial_state or ValidatorState.NEW
        self._state_entered_at: float = time.time()
        self._transition_history: List[StateTransition] = []

    def update(
        self,
        is_active: bool,
        is_ever_active: bool,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[StateTransition]:
        """
        Update state based on current validator status.

        Args:
            is_active: Whether validator is currently in active set
            is_ever_active: Whether validator has ever been in active set
            metadata: Optional metadata to include in transition record

        Returns:
            StateTransition if state changed, None otherwise
        """
        # Determine new state
        if is_active:
            new_state = ValidatorState.ACTIVE
        elif is_ever_active:
            new_state = ValidatorState.INACTIVE
        else:
            new_state = ValidatorState.NEW

        # Check if state changed
        if new_state == self.current_state:
            return None

        # Create transition record
        transition = StateTransition(
            from_state=self.current_state,
            to_state=new_state,
            validator_name=self.validator_name,
            timestamp=time.time(),
            metadata=metadata or {}
        )

        # Update state
        old_state = self.current_state
        self.current_state = new_state
        self._state_entered_at = time.time()
        self._transition_history.append(transition)

        return transition

    def get_alert_threshold(self) -> str:
        """
        Get alert threshold level based on current state.

        Returns:
            "minimal" for NEW validators
            "full" for ACTIVE validators
            "recovery" for INACTIVE validators
        """
        if self.current_state == ValidatorState.NEW:
            return "minimal"
        elif self.current_state == ValidatorState.ACTIVE:
            return "full"
        else:  # INACTIVE
            return "recovery"

    def should_alert_on(self, alert_type: str) -> bool:
        """
        Determine if an alert should be sent for the given alert type.

        Args:
            alert_type: Type of alert (e.g., "local_timeout", "node_down")

        Returns:
            True if alert should be sent, False to suppress
        """
        # Always alert on critical infrastructure issues
        if alert_type in self.ALWAYS_ALERT_TYPES:
            return True

        # For ACTIVE-only alerts, only alert if validator is ACTIVE
        if alert_type in self.ACTIVE_ONLY_ALERT_TYPES:
            return self.current_state == ValidatorState.ACTIVE

        # Default: alert based on threshold
        threshold = self.get_alert_threshold()
        if threshold == "minimal":
            return False  # Suppress most alerts for new validators
        elif threshold == "recovery":
            return False  # Suppress most alerts for inactive validators
        else:
            return True  # Full alerts for active validators

    def get_transition_history(self) -> List[StateTransition]:
        """Get list of all state transitions"""
        return list(self._transition_history)

    def get_state_duration(self) -> float:
        """Get how long the validator has been in current state (seconds)"""
        return time.time() - self._state_entered_at

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state to dictionary for persistence"""
        return {
            "validator_name": self.validator_name,
            "current_state": self.current_state.value,
            "state_entered_at": self._state_entered_at,
            "transition_count": len(self._transition_history),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ValidatorStateMachine":
        """
        Deserialize state from dictionary with corruption handling.

        If the data is corrupted (missing fields, invalid values, wrong types),
        returns a default state machine in NEW state with validator_name="unknown".

        This ensures graceful recovery from state file corruption without crashing
        the entire monitor.

        Args:
            data: Dictionary containing serialized state machine data

        Returns:
            ValidatorStateMachine instance (default state if data is corrupted)
        """
        logger = logging.getLogger(__name__)

        # Default values for corruption recovery
        DEFAULT_VALIDATOR_NAME = "unknown"
        DEFAULT_STATE = ValidatorState.NEW

        # Handle None or non-dict input
        if data is None or not isinstance(data, dict):
            logger.warning(
                f"State persistence corruption: data is not a valid dict "
                f"(type={type(data).__name__}). Returning default state."
            )
            return cls(validator_name=DEFAULT_VALIDATOR_NAME, initial_state=DEFAULT_STATE)

        # Extract validator_name with fallback
        validator_name = data.get("validator_name")
        if not isinstance(validator_name, str) or not validator_name:
            logger.warning(
                f"State persistence corruption: validator_name is missing or invalid "
                f"(value={validator_name!r}). Using default: '{DEFAULT_VALIDATOR_NAME}'"
            )
            validator_name = DEFAULT_VALIDATOR_NAME

        # Extract and validate current_state
        current_state_value = data.get("current_state")
        initial_state = DEFAULT_STATE

        if current_state_value is not None:
            try:
                initial_state = ValidatorState(current_state_value)
            except (ValueError, TypeError) as e:
                logger.warning(
                    f"State persistence corruption: current_state is invalid "
                    f"(value={current_state_value!r}, error={e}). "
                    f"Using default state: {DEFAULT_STATE.value}"
                )
                initial_state = DEFAULT_STATE
        else:
            logger.warning(
                f"State persistence corruption: current_state is missing. "
                f"Using default state: {DEFAULT_STATE.value}"
            )

        # Create machine with validated data
        machine = cls(validator_name=validator_name, initial_state=initial_state)
        machine._state_entered_at = data.get("state_entered_at", time.time())

        return machine

    # Class-level constants for file persistence
    DEFAULT_STATE_FILE: ClassVar[str] = "validator_state.json"

    def save_state(self, filepath: str) -> bool:
        """
        Save state machine to JSON file.

        Args:
            filepath: Path to the JSON file to save state

        Returns:
            True if save successful, False on any error
        """
        logger = logging.getLogger(__name__)

        try:
            path = Path(filepath)
            path.parent.mkdir(parents=True, exist_ok=True)

            data = self.to_dict()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            logger.debug(f"State saved for {self.validator_name} to {filepath}")
            return True

        except (OSError, IOError, PermissionError) as e:
            logger.warning(f"Failed to save state to {filepath}: {e}")
            return False
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialize state for {self.validator_name}: {e}")
            return False

    @classmethod
    def load_state(cls, filepath: str) -> "ValidatorStateMachine":
        """
        Load state machine from JSON file.

        If the file doesn't exist, is corrupted, or contains invalid data,
        returns a default state machine (NEW state, validator_name="unknown").

        Args:
            filepath: Path to the JSON file to load state from

        Returns:
            ValidatorStateMachine instance (default state if file missing/corrupted)
        """
        logger = logging.getLogger(__name__)

        default_machine = cls(validator_name="unknown", initial_state=ValidatorState.NEW)

        try:
            path = Path(filepath)

            if not path.exists():
                logger.debug(f"State file not found: {filepath}. Using default state.")
                return default_machine

            if path.stat().st_size == 0:
                logger.warning(f"State file is empty: {filepath}. Using default state.")
                return default_machine

            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            return cls.from_dict(data)

        except json.JSONDecodeError as e:
            logger.warning(f"State file corrupted (invalid JSON): {filepath}. Error: {e}")
            return default_machine
        except (OSError, IOError, PermissionError) as e:
            logger.warning(f"Failed to read state file {filepath}: {e}")
            return default_machine
        except Exception as e:
            logger.error(f"Unexpected error loading state from {filepath}: {e}")
            return default_machine
