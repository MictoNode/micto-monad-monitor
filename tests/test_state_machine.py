"""Tests for Validator State Machine"""

import pytest
from monad_monitor.state_machine import (
    ValidatorState,
    ValidatorStateMachine,
    StateTransition,
)


class TestValidatorState:
    """Test cases for ValidatorState enum"""

    def test_state_values(self):
        """Test that states have expected values"""
        assert ValidatorState.NEW.value == "new"
        assert ValidatorState.ACTIVE.value == "active"
        assert ValidatorState.INACTIVE.value == "inactive"

    def test_all_states_defined(self):
        """Test all expected states exist"""
        states = [s.value for s in ValidatorState]
        assert "new" in states
        assert "active" in states
        assert "inactive" in states


class TestStateTransition:
    """Test cases for StateTransition dataclass"""

    def test_transition_creation(self):
        """Test creating a transition record"""
        transition = StateTransition(
            from_state=ValidatorState.NEW,
            to_state=ValidatorState.ACTIVE,
            validator_name="TestValidator",
            timestamp=1234567890.0,
            metadata={"round_diff": 5}
        )
        assert transition.from_state == ValidatorState.NEW
        assert transition.to_state == ValidatorState.ACTIVE
        assert transition.validator_name == "TestValidator"
        assert transition.metadata["round_diff"] == 5

    def test_is_significant_transition_new_to_active(self):
        """Test NEW -> ACTIVE is significant"""
        transition = StateTransition(
            from_state=ValidatorState.NEW,
            to_state=ValidatorState.ACTIVE,
            validator_name="Test",
            timestamp=0.0,
        )
        assert transition.is_significant() is True

    def test_is_significant_transition_active_to_inactive(self):
        """Test ACTIVE -> INACTIVE is significant"""
        transition = StateTransition(
            from_state=ValidatorState.ACTIVE,
            to_state=ValidatorState.INACTIVE,
            validator_name="Test",
            timestamp=0.0,
        )
        assert transition.is_significant() is True

    def test_is_significant_transition_inactive_to_active(self):
        """Test INACTIVE -> ACTIVE is significant (re-entry)"""
        transition = StateTransition(
            from_state=ValidatorState.INACTIVE,
            to_state=ValidatorState.ACTIVE,
            validator_name="Test",
            timestamp=0.0,
        )
        assert transition.is_significant() is True

    def test_is_significant_transition_no_change(self):
        """Test same-state transition is not significant"""
        transition = StateTransition(
            from_state=ValidatorState.ACTIVE,
            to_state=ValidatorState.ACTIVE,
            validator_name="Test",
            timestamp=0.0,
        )
        assert transition.is_significant() is False

    def test_transition_alert_message_new_to_active(self):
        """Test alert message for NEW -> ACTIVE"""
        transition = StateTransition(
            from_state=ValidatorState.NEW,
            to_state=ValidatorState.ACTIVE,
            validator_name="MyValidator",
            timestamp=0.0,
        )
        msg = transition.get_alert_message()
        assert "MyValidator" in msg
        assert "entered active set" in msg.lower()

    def test_transition_alert_message_active_to_inactive(self):
        """Test alert message for ACTIVE -> INACTIVE"""
        transition = StateTransition(
            from_state=ValidatorState.ACTIVE,
            to_state=ValidatorState.INACTIVE,
            validator_name="MyValidator",
            timestamp=0.0,
            metadata={"round_diff": 15000}
        )
        msg = transition.get_alert_message()
        assert "MyValidator" in msg
        assert "dropped" in msg.lower() or "left" in msg.lower()

    def test_transition_alert_message_inactive_to_active(self):
        """Test alert message for INACTIVE -> ACTIVE"""
        transition = StateTransition(
            from_state=ValidatorState.INACTIVE,
            to_state=ValidatorState.ACTIVE,
            validator_name="MyValidator",
            timestamp=0.0,
        )
        msg = transition.get_alert_message()
        assert "MyValidator" in msg
        assert "re-entered" in msg.lower() or "back" in msg.lower()


class TestValidatorStateMachine:
    """Test cases for ValidatorStateMachine"""

    def test_initial_state_is_new(self):
        """Test that new validators start in NEW state"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        assert machine.current_state == ValidatorState.NEW

    def test_transition_new_to_active(self):
        """Test transition from NEW to ACTIVE"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        transition = machine.update(is_active=True, is_ever_active=True)

        assert machine.current_state == ValidatorState.ACTIVE
        assert transition is not None
        assert transition.from_state == ValidatorState.NEW
        assert transition.to_state == ValidatorState.ACTIVE

    def test_transition_active_to_inactive(self):
        """Test transition from ACTIVE to INACTIVE"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        machine.update(is_active=True, is_ever_active=True)  # NEW -> ACTIVE

        transition = machine.update(is_active=False, is_ever_active=True)  # ACTIVE -> INACTIVE

        assert machine.current_state == ValidatorState.INACTIVE
        assert transition is not None
        assert transition.from_state == ValidatorState.ACTIVE
        assert transition.to_state == ValidatorState.INACTIVE

    def test_transition_inactive_to_active(self):
        """Test transition from INACTIVE back to ACTIVE (re-entry)"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        machine.update(is_active=True, is_ever_active=True)  # NEW -> ACTIVE
        machine.update(is_active=False, is_ever_active=True)  # ACTIVE -> INACTIVE

        transition = machine.update(is_active=True, is_ever_active=True)  # INACTIVE -> ACTIVE

        assert machine.current_state == ValidatorState.ACTIVE
        assert transition is not None
        assert transition.from_state == ValidatorState.INACTIVE
        assert transition.to_state == ValidatorState.ACTIVE

    def test_no_transition_on_same_state(self):
        """Test that no transition occurs when state doesn't change"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        machine.update(is_active=True, is_ever_active=True)  # NEW -> ACTIVE

        transition = machine.update(is_active=True, is_ever_active=True)  # Still ACTIVE

        assert machine.current_state == ValidatorState.ACTIVE
        assert transition is None

    def test_stays_new_when_not_ever_active(self):
        """Test that validator stays NEW when never been active"""
        machine = ValidatorStateMachine(validator_name="TestValidator")

        transition = machine.update(is_active=False, is_ever_active=False)

        assert machine.current_state == ValidatorState.NEW
        assert transition is None

    def test_get_alert_threshold_new(self):
        """Test alert threshold for NEW validators is lenient"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        assert machine.get_alert_threshold() == "minimal"

    def test_get_alert_threshold_active(self):
        """Test alert threshold for ACTIVE validators is full"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        machine.update(is_active=True, is_ever_active=True)
        assert machine.get_alert_threshold() == "full"

    def test_get_alert_threshold_inactive(self):
        """Test alert threshold for INACTIVE validators is recovery"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        machine.update(is_active=True, is_ever_active=True)
        machine.update(is_active=False, is_ever_active=True)
        assert machine.get_alert_threshold() == "recovery"

    def test_should_alert_local_timeout_new_validator(self):
        """Test that local_timeout alerts are suppressed for NEW validators"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        assert machine.should_alert_on("local_timeout") is False

    def test_should_alert_local_timeout_active_validator(self):
        """Test that local_timeout alerts are enabled for ACTIVE validators"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        machine.update(is_active=True, is_ever_active=True)
        assert machine.should_alert_on("local_timeout") is True

    def test_should_alert_local_timeout_inactive_validator(self):
        """Test that local_timeout alerts are suppressed for INACTIVE validators"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        machine.update(is_active=True, is_ever_active=True)
        machine.update(is_active=False, is_ever_active=True)
        assert machine.should_alert_on("local_timeout") is False

    def test_should_alert_node_down_always_true(self):
        """Test that node_down alerts are always enabled"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        assert machine.should_alert_on("node_down") is True

        machine.update(is_active=True, is_ever_active=True)
        assert machine.should_alert_on("node_down") is True

        machine.update(is_active=False, is_ever_active=True)
        assert machine.should_alert_on("node_down") is True

    def test_transition_history(self):
        """Test that transition history is tracked"""
        machine = ValidatorStateMachine(validator_name="TestValidator")

        machine.update(is_active=True, is_ever_active=True)
        machine.update(is_active=False, is_ever_active=True)
        machine.update(is_active=True, is_ever_active=True)

        history = machine.get_transition_history()
        assert len(history) == 3  # 3 transitions

    def test_get_state_duration(self):
        """Test getting time in current state"""
        import time

        machine = ValidatorStateMachine(validator_name="TestValidator")
        time.sleep(0.1)  # Small delay

        duration = machine.get_state_duration()
        assert duration >= 0.1

    def test_persistence_save_load(self):
        """Test saving and loading state from dict"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        machine.update(is_active=True, is_ever_active=True)

        # Save state
        state_dict = machine.to_dict()
        assert state_dict["current_state"] == "active"
        assert state_dict["validator_name"] == "TestValidator"

        # Load into new machine
        machine2 = ValidatorStateMachine.from_dict(state_dict)
        assert machine2.current_state == ValidatorState.ACTIVE
        assert machine2.validator_name == "TestValidator"

    def test_multiple_validators_independent(self):
        """Test that multiple state machines are independent"""
        machine1 = ValidatorStateMachine(validator_name="Validator1")
        machine2 = ValidatorStateMachine(validator_name="Validator2")

        machine1.update(is_active=True, is_ever_active=True)
        # machine2 stays NEW

        assert machine1.current_state == ValidatorState.ACTIVE
        assert machine2.current_state == ValidatorState.NEW


class TestStatePersistenceCorruption:
    """Test cases for state persistence corruption handling (Season 5.1)"""

    def test_from_dict_handles_missing_validator_name(self):
        """Test that missing validator_name uses default name but preserves state"""
        corrupted_data = {"current_state": "active"}  # Missing validator_name

        machine = ValidatorStateMachine.from_dict(corrupted_data)

        # Should use default name but preserve valid state
        assert machine.current_state == ValidatorState.ACTIVE  # State preserved
        assert machine.validator_name == "unknown"  # Default name used

    def test_from_dict_handles_missing_current_state(self):
        """Test that missing current_state returns default state"""
        corrupted_data = {"validator_name": "TestValidator"}  # Missing current_state

        machine = ValidatorStateMachine.from_dict(corrupted_data)

        # Should return default state on corruption
        assert machine.current_state == ValidatorState.NEW
        assert machine.validator_name == "TestValidator"

    def test_from_dict_handles_invalid_state_value(self):
        """Test that invalid state value returns default state"""
        corrupted_data = {
            "validator_name": "TestValidator",
            "current_state": "invalid_state_value",  # Not a valid ValidatorState
        }

        machine = ValidatorStateMachine.from_dict(corrupted_data)

        # Should return default state on corruption
        assert machine.current_state == ValidatorState.NEW

    def test_from_dict_handles_empty_dict(self):
        """Test that empty dict returns default state"""
        corrupted_data = {}

        machine = ValidatorStateMachine.from_dict(corrupted_data)

        assert machine.current_state == ValidatorState.NEW
        assert machine.validator_name == "unknown"

    def test_from_dict_handles_none_input(self):
        """Test that None input returns default state"""
        machine = ValidatorStateMachine.from_dict(None)

        assert machine.current_state == ValidatorState.NEW
        assert machine.validator_name == "unknown"

    def test_from_dict_handles_non_dict_input(self):
        """Test that non-dict input returns default state"""
        machine = ValidatorStateMachine.from_dict("not a dict")

        assert machine.current_state == ValidatorState.NEW
        assert machine.validator_name == "unknown"

    def test_from_dict_handles_extra_fields_gracefully(self):
        """Test that extra fields are ignored (not corrupted)"""
        data = {
            "validator_name": "TestValidator",
            "current_state": "active",
            "state_entered_at": 1234567890.0,
            "extra_field": "should be ignored",
            "another_extra": 12345,
        }

        machine = ValidatorStateMachine.from_dict(data)

        # Should work fine with extra fields
        assert machine.current_state == ValidatorState.ACTIVE
        assert machine.validator_name == "TestValidator"

    def test_from_dict_handles_wrong_types(self):
        """Test that wrong types for fields return default state"""
        corrupted_data = {
            "validator_name": 12345,  # Should be string
            "current_state": ["active"],  # Should be string
        }

        machine = ValidatorStateMachine.from_dict(corrupted_data)

        # Should return default state on type mismatch
        assert machine.current_state == ValidatorState.NEW


class TestFilePersistence:
    """Test cases for file-based state persistence (Session 19)"""

    def test_save_state_to_file(self, tmp_path):
        """Test saving state machine to JSON file"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        machine.update(is_active=True, is_ever_active=True)

        filepath = str(tmp_path / "state.json")
        result = machine.save_state(filepath)

        assert result is True
        assert tmp_path.joinpath("state.json").exists()

    def test_save_state_creates_valid_json(self, tmp_path):
        """Test that saved file contains valid JSON"""
        import json

        machine = ValidatorStateMachine(validator_name="TestValidator")
        machine.update(is_active=True, is_ever_active=True)

        filepath = str(tmp_path / "state.json")
        machine.save_state(filepath)

        with open(filepath, "r") as f:
            data = json.load(f)

        assert data["validator_name"] == "TestValidator"
        assert data["current_state"] == "active"

    def test_load_state_from_file(self, tmp_path):
        """Test loading state machine from JSON file"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        machine.update(is_active=True, is_ever_active=True)

        filepath = str(tmp_path / "state.json")
        machine.save_state(filepath)

        # Load into new instance
        loaded = ValidatorStateMachine.load_state(filepath)

        assert loaded.current_state == ValidatorState.ACTIVE
        assert loaded.validator_name == "TestValidator"

    def test_load_state_missing_file_returns_default(self, tmp_path):
        """Test that missing file returns default state machine"""
        filepath = str(tmp_path / "nonexistent.json")
        machine = ValidatorStateMachine.load_state(filepath)

        # Should return default state (NEW) with unknown name
        assert machine.current_state == ValidatorState.NEW
        assert machine.validator_name == "unknown"

    def test_load_state_corrupted_file_returns_default(self, tmp_path):
        """Test that corrupted JSON file returns default state machine"""
        filepath = str(tmp_path / "corrupted.json")
        with open(filepath, "w") as f:
            f.write("{ invalid json content")

        machine = ValidatorStateMachine.load_state(filepath)

        assert machine.current_state == ValidatorState.NEW
        assert machine.validator_name == "unknown"

    def test_load_state_empty_file_returns_default(self, tmp_path):
        """Test that empty file returns default state machine"""
        filepath = str(tmp_path / "empty.json")
        with open(filepath, "w") as f:
            f.write("")

        machine = ValidatorStateMachine.load_state(filepath)

        assert machine.current_state == ValidatorState.NEW
        assert machine.validator_name == "unknown"

    def test_save_state_handles_permission_error(self, tmp_path):
        """Test that save_state returns False on permission error"""
        import os
        import stat
        import sys

        # Skip on Windows - permission model differs significantly
        if sys.platform == "win32":
            pytest.skip("Permission test not reliable on Windows")

        machine = ValidatorStateMachine(validator_name="TestValidator")

        # Create read-only directory
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        os.chmod(readonly_dir, stat.S_IRUSR | stat.S_IXUSR)  # read + execute only

        filepath = str(readonly_dir / "state.json")
        result = machine.save_state(filepath)

        # Should return False on permission error (not raise)
        assert result is False

        # Cleanup - restore permissions for temp dir cleanup
        os.chmod(readonly_dir, stat.S_IRWXU)

    def test_save_and_load_preserves_inactive_state(self, tmp_path):
        """Test that INACTIVE state is preserved through save/load cycle"""
        machine = ValidatorStateMachine(validator_name="TestValidator")
        machine.update(is_active=True, is_ever_active=True)  # NEW -> ACTIVE
        machine.update(is_active=False, is_ever_active=True)  # ACTIVE -> INACTIVE

        filepath = str(tmp_path / "state.json")
        machine.save_state(filepath)

        loaded = ValidatorStateMachine.load_state(filepath)

        assert loaded.current_state == ValidatorState.INACTIVE
        assert loaded.validator_name == "TestValidator"
