"""Tests for the active-set verdict -> state machine handoff (main.py)

The verdict comes from Huginn, gmonads or local inference. When no source can
answer, the verdict is None and the state must not move: coercing it to
"inactive" fired a spurious LEFT ACTIVE SET alert and muted the active-only
alerts until the sources recovered.
"""

from monad_monitor.main import apply_active_set_transition
from monad_monitor.state_machine import ValidatorState, ValidatorStateMachine


def make_machine(state: ValidatorState = ValidatorState.NEW) -> ValidatorStateMachine:
    """A machine already in the given state, as one loaded from persisted state"""
    return ValidatorStateMachine(validator_name="validator-1", initial_state=state)


class TestUnknownVerdictHoldsState:
    """A None verdict means no source could answer - nothing may move"""

    def test_unknown_verdict_does_not_leave_the_active_set(self):
        machine = make_machine(ValidatorState.ACTIVE)

        transition = apply_active_set_transition(machine, "validator-1", None, True)

        assert transition is None
        assert machine.current_state == ValidatorState.ACTIVE

    def test_unknown_verdict_does_not_re_enter_the_active_set(self):
        machine = make_machine(ValidatorState.INACTIVE)

        transition = apply_active_set_transition(machine, "validator-1", None, True)

        assert transition is None
        assert machine.current_state == ValidatorState.INACTIVE

    def test_unknown_verdict_keeps_a_new_machine_new(self):
        machine = make_machine(ValidatorState.NEW)

        assert apply_active_set_transition(machine, "validator-1", None, False) is None
        assert machine.current_state == ValidatorState.NEW


class TestVerdictTransitions:
    """Verified verdicts keep driving the state machine exactly as before"""

    def test_first_verdict_initializes_without_alerting(self):
        machine = make_machine(ValidatorState.NEW)

        transition = apply_active_set_transition(machine, "validator-1", True, True)

        assert transition is None
        assert machine.current_state == ValidatorState.ACTIVE

    def test_first_verdict_initializes_inactive_from_history(self):
        machine = make_machine(ValidatorState.NEW)

        apply_active_set_transition(machine, "validator-1", False, True)

        assert machine.current_state == ValidatorState.INACTIVE

    def test_left_active_set_is_reported(self):
        machine = make_machine(ValidatorState.ACTIVE)

        transition = apply_active_set_transition(machine, "validator-1", False, True)

        assert transition is not None
        assert transition.from_state == ValidatorState.ACTIVE
        assert transition.to_state == ValidatorState.INACTIVE

    def test_re_entry_is_reported(self):
        machine = make_machine(ValidatorState.INACTIVE)

        transition = apply_active_set_transition(machine, "validator-1", True, True)

        assert transition is not None
        assert transition.from_state == ValidatorState.INACTIVE
        assert transition.to_state == ValidatorState.ACTIVE

    def test_new_machine_without_history_stays_new(self):
        machine = make_machine(ValidatorState.NEW)

        transition = apply_active_set_transition(machine, "validator-1", False, False)

        assert transition is None
        assert machine.current_state == ValidatorState.NEW
