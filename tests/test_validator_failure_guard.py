"""Tests for handle_validator_failure (main.py)

One validator's unexpected exception must not end the monitor: the loop keeps
checking every other validator, and the failing one stays visible on the
dashboard as unhealthy until its next successful cycle.
"""

from typing import Any, Dict
from unittest.mock import MagicMock

from monad_monitor.main import handle_validator_failure


class TestValidatorFailureBookkeeping:
    def setup_method(self):
        self.validator = MagicMock()
        self.validator.name = "validator-1"
        self.validator.network = "testnet"
        self.state: Dict[str, Any] = {"fails": 2, "last_height": 10, "last_peers": 5}
        self.payload: Dict[str, Dict[str, Any]] = {}

    def test_failure_is_counted_and_published_as_unhealthy(self):
        handle_validator_failure(
            self.validator,
            self.state,
            self.payload,
            RuntimeError("unexpected payload shape"),
            "active",
        )

        assert self.state["fails"] == 3

        entry = self.payload["validator-1"]
        assert entry["healthy"] is False
        assert entry["state"] == "active"
        assert entry["fails"] == 3
        assert entry["network"] == "testnet"
        assert entry["height"] == 10
        assert entry["peers"] == 5
        assert entry["last_check"] > 0
        assert entry["warnings"] == []
        assert entry["criticals"] == []
        assert entry["error"]

    def test_other_validators_are_left_alone(self):
        self.payload["validator-2"] = {"healthy": True, "fails": 0}

        handle_validator_failure(
            self.validator, self.state, self.payload, ValueError("bad"), "new"
        )

        assert self.payload["validator-2"] == {"healthy": True, "fails": 0}
