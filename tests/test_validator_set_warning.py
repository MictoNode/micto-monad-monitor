"""Tests for the M4 next-epoch active set exit warning (Huginn staking data)"""

import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import responses

from monad_monitor.alerts import AlertHandler
from monad_monitor.config import ValidatorConfig
from monad_monitor.huginn import ValidatorSetState
from monad_monitor.main import warn_if_leaving_next_epoch


def make_validator_set(
    epoch: Optional[int] = 1233,
    leaving_ids: Optional[set] = None,
    leaving: Optional[List[Dict[str, Any]]] = None,
    in_delay_period: bool = False,
) -> ValidatorSetState:
    """Build a ValidatorSetState shaped like the Huginn staking API response"""
    if leaving_ids is None:
        leaving_ids = {67}
    if leaving is None:
        leaving = [{"validator_id": 67, "name": "Unity Nodes", "stake": 11000000}]
    return ValidatorSetState(
        network="testnet",
        epoch=epoch,
        in_delay_period=in_delay_period,
        counts={"leaving": len(leaving_ids)},
        leaving_ids=leaving_ids,
        leaving=leaving,
        fetched_at=time.time(),
    )


class TestValidatorSetWarning:
    """Unit tests for warn_if_leaving_next_epoch"""

    def setup_method(self):
        self.validator = ValidatorConfig(
            name="validator-1",
            host="192.168.1.100",
            metrics_port=8889,
            rpc_port=8080,
            node_exporter_port=None,
            validator_secp="0x1234",
            enabled=True,
            network="testnet",
        )
        self.state: Dict[str, Any] = {"last_validator_set_warning_epoch": None}
        self.huginn_data: Dict[str, Any] = {"is_active": True}
        self.alerts = MagicMock()
        self.alerts.alert_warning.return_value = True
        self.client = MagicMock()
        self.client.get_validator_id.return_value = 67

    def _warn(self, enabled: bool = True, validator_set=None, huginn_data=None):
        return warn_if_leaving_next_epoch(
            enabled=enabled,
            validator=self.validator,
            state=self.state,
            huginn_data=self.huginn_data if huginn_data is None else huginn_data,
            validator_set=validator_set or make_validator_set(),
            huginn_client=self.client,
            alerts=self.alerts,
        )

    def test_warning_sent_when_leaving_next_epoch(self):
        result = self._warn()

        assert result is True
        self.alerts.alert_warning.assert_called_once()
        message = self.alerts.alert_warning.call_args[0][0]
        assert "validator-1" in message
        assert "Leaving Active Set Next Epoch" in message
        assert "validator id 67" in message
        assert "epoch 1233" in message
        assert "stake 11000000" in message
        # Warned epoch remembered so the same epoch is not re-alerted
        assert self.state["last_validator_set_warning_epoch"] == 1233
        # WARNING channel set only - Pushover is never used for M4
        self.alerts.send_pushover.assert_not_called()

    def test_warning_not_repeated_in_same_epoch(self):
        self._warn()
        second = self._warn()

        assert second is False
        self.alerts.alert_warning.assert_called_once()
        assert self.state["last_validator_set_warning_epoch"] == 1233

    def test_warning_sent_again_for_a_new_epoch(self):
        self._warn()

        new_set = make_validator_set(
            epoch=1234,
            leaving_ids={67},
            leaving=[{"validator_id": 67, "name": "Unity Nodes", "stake": 11000000}],
        )
        second = self._warn(validator_set=new_set)

        assert second is True
        assert self.alerts.alert_warning.call_count == 2
        assert self.state["last_validator_set_warning_epoch"] == 1234

    def test_no_warning_when_validator_not_in_leaving_list(self):
        other_set = make_validator_set(
            leaving_ids={68},
            leaving=[{"validator_id": 68, "name": "Other", "stake": 5000000}],
        )

        result = self._warn(validator_set=other_set)

        assert result is False
        self.alerts.alert_warning.assert_not_called()
        assert self.state["last_validator_set_warning_epoch"] is None

    def test_no_warning_when_disabled(self):
        result = self._warn(enabled=False)

        assert result is False
        self.alerts.alert_warning.assert_not_called()

    def test_no_warning_when_validator_not_active(self):
        result = self._warn(huginn_data={"is_active": False})

        assert result is False
        self.alerts.alert_warning.assert_not_called()

    def test_no_warning_when_validator_set_unavailable(self):
        result = warn_if_leaving_next_epoch(
            enabled=True,
            validator=self.validator,
            state=self.state,
            huginn_data=self.huginn_data,
            validator_set=None,
            huginn_client=self.client,
            alerts=self.alerts,
        )

        assert result is False
        self.alerts.alert_warning.assert_not_called()

    def test_no_warning_when_validator_id_unresolved(self):
        self.client.get_validator_id.return_value = None

        result = self._warn()

        assert result is False
        self.alerts.alert_warning.assert_not_called()

    def test_delay_period_wording_is_appended(self):
        delay_set = make_validator_set(in_delay_period=True)

        result = self._warn(validator_set=delay_set)

        assert result is True
        message = self.alerts.alert_warning.call_args[0][0]
        assert "delay period" in message
        assert "one further epoch" in message

    def test_warning_uses_non_pushover_channels(self):
        """End-to-end through AlertHandler: Telegram only, never Pushover"""
        alerts = AlertHandler(
            telegram_token="test-token",
            telegram_chat_id="test-chat",
            pushover_user_key="test-user",
            pushover_app_token="test-app",
        )

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-token/sendMessage",
                json={"ok": True},
                status=200,
            )

            result = warn_if_leaving_next_epoch(
                enabled=True,
                validator=self.validator,
                state=self.state,
                huginn_data=self.huginn_data,
                validator_set=make_validator_set(),
                huginn_client=self.client,
                alerts=alerts,
            )

            assert result is True
            assert len(rsps.calls) == 1
            assert "api.pushover.net" not in rsps.calls[0].request.url
            assert "Leaving Active Set Next Epoch" in str(rsps.calls[0].request.body)
