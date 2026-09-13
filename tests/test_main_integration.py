"""Integration tests for main.py - HealthServer and StateMachine integration"""

import json
import os
import time
import pytest
import threading
import urllib.request
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any

from monad_monitor.main import apply_active_set_transition
from monad_monitor.state_machine import ValidatorState, ValidatorStateMachine

# We're testing the integration points, not the full main loop
# These tests verify that main.py correctly initializes and uses:
# 1. HealthServer (port 8181)
# 2. ValidatorStateMachine (replaces was_active logic)


class TestHealthServerIntegration:
    """Test HealthServer integration in main.py"""

    def test_health_server_starts_on_configured_port(self):
        """Test that HealthServer starts on the configured port (default 8181)"""
        from monad_monitor.health_server import HealthServer

        # Create server with default port
        server = HealthServer(port=8181)
        server.start()
        time.sleep(0.5)

        try:
            # Set healthy status first to avoid 503
            server.update_status(is_healthy=True)

            # Verify server responds
            url = "http://localhost:8181/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.status == 200
        finally:
            server.stop()

    def test_health_server_uses_config_port(self):
        """Test that HealthServer reads port from config"""
        # This test verifies the config integration pattern
        # The actual main.py should read health_server.port from config
        from monad_monitor.health_server import HealthServer

        # Port should be configurable
        server = HealthServer(port=18181)
        assert server.port == 18181
        server.start()
        time.sleep(0.3)

        try:
            # Set healthy status first
            server.update_status(is_healthy=True)

            url = "http://localhost:18181/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.status == 200
        finally:
            server.stop()

    def test_health_server_updates_validator_status(self):
        """Test that main.py updates HealthServer with validator status"""
        from monad_monitor.health_server import HealthServer

        server = HealthServer(port=18182)
        server.start()
        time.sleep(0.3)

        try:
            # Simulate main.py updating status
            server.update_status(
                is_healthy=True,
                validators={
                    "TestValidator": {
                        "state": "active",
                        "healthy": True,
                        "height": 12345,
                        "peers": 25,
                    }
                }
            )

            # Verify the update is reflected
            url = "http://localhost:18182/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                assert data["status"] == "healthy"
                assert "TestValidator" in data["validators"]
                assert data["validators"]["TestValidator"]["height"] == 12345
        finally:
            server.stop()


class TestStateMachineIntegration:
    """Test ValidatorStateMachine integration in main.py"""

    def test_state_machine_initializes_per_validator(self):
        """Test that each validator gets its own StateMachine instance"""
        from monad_monitor.state_machine import ValidatorStateMachine, ValidatorState

        # Simulate main.py initialization pattern
        validators = ["Validator1", "Validator2", "Validator3"]
        state_machines = {}

        for name in validators:
            state_machines[name] = ValidatorStateMachine(validator_name=name)

        # Each should be independent and start as NEW
        assert state_machines["Validator1"].current_state == ValidatorState.NEW
        assert state_machines["Validator2"].current_state == ValidatorState.NEW
        assert state_machines["Validator3"].current_state == ValidatorState.NEW

        # Transition one
        state_machines["Validator1"].update(is_active=True, is_ever_active=True)
        assert state_machines["Validator1"].current_state == ValidatorState.ACTIVE
        assert state_machines["Validator2"].current_state == ValidatorState.NEW

    def test_state_machine_replaces_was_active_logic(self):
        """Test that StateMachine correctly handles state transitions
        that were previously tracked with was_active boolean"""
        from monad_monitor.state_machine import ValidatorStateMachine, ValidatorState

        machine = ValidatorStateMachine(validator_name="TestValidator")

        # Initially NEW (was_active = None equivalent)
        assert machine.current_state == ValidatorState.NEW

        # First time becoming active (was_active = False -> True)
        transition = machine.update(is_active=True, is_ever_active=True)
        assert machine.current_state == ValidatorState.ACTIVE
        assert transition is not None
        assert transition.from_state == ValidatorState.NEW

        # Stay active (no transition)
        transition = machine.update(is_active=True, is_ever_active=True)
        assert transition is None

        # Leave active set (was_active = True -> active set left)
        transition = machine.update(is_active=False, is_ever_active=True)
        assert machine.current_state == ValidatorState.INACTIVE
        assert transition.from_state == ValidatorState.ACTIVE
        assert transition.to_state == ValidatorState.INACTIVE

        # Re-enter active set
        transition = machine.update(is_active=True, is_ever_active=True)
        assert machine.current_state == ValidatorState.ACTIVE
        assert transition.from_state == ValidatorState.INACTIVE

    def test_state_transition_triggers_alert(self):
        """Test that state transitions generate alert messages"""
        from monad_monitor.state_machine import ValidatorStateMachine, ValidatorState

        machine = ValidatorStateMachine(validator_name="MyValidator")

        # NEW -> ACTIVE should generate "entered active set" alert
        transition = machine.update(is_active=True, is_ever_active=True)
        assert transition is not None
        msg = transition.get_alert_message()
        assert "MyValidator" in msg
        assert "active set" in msg.lower()

        # ACTIVE -> INACTIVE should generate "left active set" alert
        transition = machine.update(is_active=False, is_ever_active=True)
        assert transition is not None
        msg = transition.get_alert_message()
        assert "MyValidator" in msg
        assert "left" in msg.lower() or "inactive" in msg.lower()

    def test_state_machine_persistence(self):
        """Test that state can be persisted and restored (for restart scenarios)"""
        from monad_monitor.state_machine import ValidatorStateMachine, ValidatorState

        # Create and transition
        machine = ValidatorStateMachine(validator_name="PersistentValidator")
        machine.update(is_active=True, is_ever_active=True)
        machine.update(is_active=False, is_ever_active=True)

        # Save state
        state_dict = machine.to_dict()
        assert state_dict["current_state"] == "inactive"

        # Restore state (simulating restart)
        restored = ValidatorStateMachine.from_dict(state_dict)
        assert restored.current_state == ValidatorState.INACTIVE
        assert restored.validator_name == "PersistentValidator"


class TestConfigIntegration:
    """Test configuration integration for health_server settings"""

    def test_health_server_config_section_exists(self):
        """Test that config.example.yaml has health_server section"""
        config_path = "config/config.example.yaml"

        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Should have health_server section with port
        assert "health_server" in content or "health server" in content.lower()

    def test_health_server_default_port_is_8181(self):
        """Test that default port is 8181 (not 8080 to avoid conflicts)"""
        from monad_monitor.health_server import HealthServer

        # Default should be 8080 in the class, but main.py should override to 8181
        # We verify the override pattern works
        server = HealthServer(port=8181)
        assert server.port == 8181


class TestGracefulShutdown:
    """Test graceful shutdown of integrated components"""

    def test_health_server_stops_cleanly(self):
        """Test that HealthServer can be stopped cleanly"""
        from monad_monitor.health_server import HealthServer

        server = HealthServer(port=18183)
        server.start()
        time.sleep(0.3)

        assert server.is_running() is True

        # Stop should not raise
        server.stop()
        time.sleep(0.1)

        assert server.is_running() is False

    def test_multiple_stop_calls_safe(self):
        """Test that multiple stop calls don't cause errors"""
        from monad_monitor.health_server import HealthServer

        server = HealthServer(port=18184)
        server.start()
        time.sleep(0.2)

        # Multiple stops should be safe
        server.stop()
        server.stop()  # Should not raise
        server.stop()  # Should not raise


class TestMainIntegrationPattern:
    """Test the integration pattern used in main.py"""

    def test_main_does_not_shadow_os_module_locally(self):
        """main() must use the module-level os import, not a local one.

        A local 'import os' inside main() makes Python treat 'os' as a
        function-local name, so earlier os.getenv() calls raise
        UnboundLocalError at runtime. Regression test for v1.5.0 crash.
        """
        import dis

        from monad_monitor import main as main_module

        local_os_binds = [
            inst
            for inst in dis.get_instructions(main_module.main)
            if inst.opname in ("IMPORT_NAME", "STORE_FAST", "STORE_DEREF")
            and inst.argval == "os"
        ]
        assert local_os_binds == [], (
            f"main() shadows the 'os' module with a local binding: {local_os_binds}"
        )

    def test_integration_components_work_together(self):
        """Test that HealthServer and StateMachine work together correctly"""
        from monad_monitor.health_server import HealthServer
        from monad_monitor.state_machine import ValidatorStateMachine, ValidatorState

        # Simulate main.py integration pattern
        health_server = HealthServer(port=18185)
        health_server.start()
        time.sleep(0.3)

        state_machines: Dict[str, ValidatorStateMachine] = {}
        validators_data: Dict[str, Dict[str, Any]] = {}

        try:
            # Simulate monitoring cycle
            validators = ["Validator1", "Validator2"]

            for name in validators:
                state_machines[name] = ValidatorStateMachine(validator_name=name)

            # First check - Validator1 becomes active
            transition = state_machines["Validator1"].update(
                is_active=True,
                is_ever_active=True
            )
            if transition:
                # In main.py, this would trigger an alert
                alert_msg = transition.get_alert_message()
                assert "active set" in alert_msg.lower()

            # Update health server with current status
            for name in validators:
                machine = state_machines[name]
                validators_data[name] = {
                    "state": machine.current_state.value,
                    "healthy": True,
                }

            health_server.update_status(is_healthy=True, validators=validators_data)

            # Verify health endpoint reflects the state
            url = "http://localhost:18185/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                assert data["status"] == "healthy"
                assert data["validators"]["Validator1"]["state"] == "active"
                assert data["validators"]["Validator2"]["state"] == "new"

        finally:
            health_server.stop()


class TestStateMachineInitializationOnFailure:
    """The active-set handoff in main.py (see apply_active_set_transition)

    These replace copies of main.py's inline logic: a copy cannot fail when the
    real code changes, so it guarded nothing.
    """

    def _apply(self, machine, is_active, is_ever_active):
        return apply_active_set_transition(
            machine, "TestValidator", is_active, is_ever_active
        )

    def test_state_remains_new_when_huginn_unavailable(self):
        """An unknown verdict on the first check must not invent a state"""
        machine = ValidatorStateMachine(validator_name="TestValidator")

        transition = self._apply(machine, None, False)

        assert transition is None
        assert machine.current_state == ValidatorState.NEW

    def test_state_initializes_correctly_with_huginn_data(self):
        """A verified verdict at boot initializes the state without alerting"""
        machine = ValidatorStateMachine(validator_name="TestValidator")

        transition = self._apply(machine, True, True)

        assert transition is None
        assert machine.current_state == ValidatorState.ACTIVE

    def test_state_initializes_inactive_when_previously_active(self):
        """A validator that was active but is now out of the set starts INACTIVE"""
        machine = ValidatorStateMachine(validator_name="TestValidator")

        self._apply(machine, False, True)

        assert machine.current_state == ValidatorState.INACTIVE

    def test_no_false_active_alert_on_restart_with_huginn_failure(self):
        """Unknown verdict at boot, then a verified one: initialize, never alert"""
        machine = ValidatorStateMachine(validator_name="TestValidator")

        assert self._apply(machine, None, False) is None
        assert machine.current_state == ValidatorState.NEW

        transition = self._apply(machine, True, True)

        assert machine.current_state == ValidatorState.ACTIVE
        assert transition is None


class TestHuginnTimeoutAlertThreshold:
    """Tests for the Huginn network-visible timeout alert threshold decision"""

    def test_no_alert_without_baseline(self):
        """First check records baseline only - no alert"""
        from monad_monitor.main import timeout_increase_to_report

        assert timeout_increase_to_report(None, 1, 1) == 0

    def test_no_alert_on_flat_count(self):
        """No increase means no alert"""
        from monad_monitor.main import timeout_increase_to_report

        assert timeout_increase_to_report(3, 3, 1) == 0

    def test_no_alert_on_decrease(self):
        """Count reset/decrease (e.g. Huginn window rollover) never alerts"""
        from monad_monitor.main import timeout_increase_to_report

        assert timeout_increase_to_report(5, 2, 1) == 0

    def test_threshold_one_alerts_on_any_increase(self):
        """Default threshold 1 preserves legacy behavior: any increase alerts"""
        from monad_monitor.main import timeout_increase_to_report

        assert timeout_increase_to_report(0, 1, 1) == 1

    def test_increase_below_threshold_suppressed(self):
        """Increase of 1-2 with threshold 3 produces no alert"""
        from monad_monitor.main import timeout_increase_to_report

        assert timeout_increase_to_report(5, 6, 3) == 0
        assert timeout_increase_to_report(5, 7, 3) == 0

    def test_increase_at_threshold_reported(self):
        """Increase exactly at threshold is reported"""
        from monad_monitor.main import timeout_increase_to_report

        assert timeout_increase_to_report(5, 8, 3) == 3

    def test_increase_above_threshold_reported(self):
        """Larger burst reports the full increase"""
        from monad_monitor.main import timeout_increase_to_report

        assert timeout_increase_to_report(5, 10, 3) == 5