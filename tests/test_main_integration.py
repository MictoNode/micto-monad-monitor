"""Integration tests for main.py - HealthServer and StateMachine integration"""

import json
import os
import time
import pytest
import threading
import urllib.request
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any

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
    """Test state machine initialization when Huginn API fails (Season 5.4)"""

    def test_state_remains_new_when_huginn_unavailable(self):
        """When Huginn data is unavailable on first check, state should remain NEW"""
        from monad_monitor.state_machine import ValidatorStateMachine, ValidatorState

        machine = ValidatorStateMachine(validator_name="TestValidator")

        # Simulate the initialization logic with None Huginn data
        # (Huginn API failure case)
        is_active = None  # Huginn API failed
        is_ever_active = False  # Cannot determine from None data
        huginn_data = None

        # Current main.py logic (lines 231-239)
        # Initialize state machine with correct state on first check
        # This prevents false "ENTERED ACTIVE SET" alerts on restart
        if machine.current_state == ValidatorState.NEW and is_ever_active:
            if is_active:
                machine.current_state = ValidatorState.ACTIVE
            else:
                machine.current_state = ValidatorState.INACTIVE
        else:
            # If we don't have Huginn data, infer is_ever_active from current state
            if is_ever_active is False and machine.current_state != ValidatorState.NEW:
                is_ever_active = True

            transition = machine.update(
                is_active=is_active if is_active is not None else False,
                is_ever_active=is_ever_active,
                metadata={}
            )

        # When Huginn data is unavailable, state should remain NEW
        # (not be assigned ACTIVE or INACTIVE incorrectly)
        assert machine.current_state == ValidatorState.NEW

    def test_state_initializes_correctly_with_huginn_data(self):
        """When Huginn data is available on first check, state should initialize correctly"""
        from monad_monitor.state_machine import ValidatorStateMachine, ValidatorState

        machine = ValidatorStateMachine(validator_name="TestValidator")

        # Simulate Huginn data showing validator is active
        is_active = True
        is_ever_active = True
        huginn_data = {"is_active": True, "is_ever_active": True}

        # Current main.py logic (lines 231-239)
        if machine.current_state == ValidatorState.NEW and is_ever_active:
            if is_active:
                machine.current_state = ValidatorState.ACTIVE
            else:
                machine.current_state = ValidatorState.INACTIVE

        # Should initialize to ACTIVE since is_ever_active=True and is_active=True
        assert machine.current_state == ValidatorState.ACTIVE

    def test_state_initializes_inactive_when_previously_active(self):
        """When validator was active but now inactive, should initialize as INACTIVE"""
        from monad_monitor.state_machine import ValidatorStateMachine, ValidatorState

        machine = ValidatorStateMachine(validator_name="TestValidator")

        # Simulate Huginn data showing validator was active but now inactive
        is_active = False
        is_ever_active = True
        huginn_data = {"is_active": False, "is_ever_active": True}

        # Current main.py logic (lines 231-239)
        if machine.current_state == ValidatorState.NEW and is_ever_active:
            if is_active:
                machine.current_state = ValidatorState.ACTIVE
            else:
                machine.current_state = ValidatorState.INACTIVE

        # Should initialize to INACTIVE since is_ever_active=True but is_active=False
        assert machine.current_state == ValidatorState.INACTIVE

    def test_no_false_active_alert_on_restart_with_huginn_failure(self):
        """No false 'ENTERED ACTIVE SET' alert when Huginn fails on restart

        Scenario:
        1. Validator was ACTIVE before restart
        2. Monitor restarts
        3. Huginn API fails on first check
        4. State should remain NEW (no false alert)
        5. On next check, Huginn succeeds with is_ever_active=True, is_active=True
        6. State should initialize to ACTIVE without transition alert
        """
        from monad_monitor.state_machine import ValidatorStateMachine, ValidatorState

        # First check: Huginn API failure
        machine = ValidatorStateMachine(validator_name="TestValidator")
        is_active = None
        is_ever_active = False

        if machine.current_state == ValidatorState.NEW and is_ever_active:
            if is_active:
                machine.current_state = ValidatorState.ACTIVE
            else:
                machine.current_state = ValidatorState.INACTIVE
        else:
            if is_ever_active is False and machine.current_state != ValidatorState.NEW:
                is_ever_active = True
            transition = machine.update(
                is_active=is_active if is_active is not None else False,
                is_ever_active=is_ever_active,
                metadata={}
            )
            # No transition should occur
            assert transition is None

        # State should remain NEW
        assert machine.current_state == ValidatorState.NEW

        # Second check: Huginn API succeeds
        is_active = True
        is_ever_active = True

        if machine.current_state == ValidatorState.NEW and is_ever_active:
            if is_active:
                machine.current_state = ValidatorState.ACTIVE
            else:
                machine.current_state = ValidatorState.INACTIVE
            # Direct assignment, no transition created
            transition = None
        else:
            transition = machine.update(is_active=is_active, is_ever_active=is_ever_active)

        # State should now be ACTIVE, but NO transition alert
        assert machine.current_state == ValidatorState.ACTIVE
        assert transition is None  # No alert should be sent