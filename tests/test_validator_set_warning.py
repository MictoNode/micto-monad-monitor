"""Tests for the M4 next-epoch active set exit warning (Huginn staking data)"""

import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import responses

from monad_monitor.alerts import AlertHandler
from monad_monitor.config import ValidatorConfig
from monad_monitor.huginn import ValidatorSetState
from monad_monitor.main import notify_if_entering_next_epoch, warn_if_leaving_next_epoch


def make_validator_set(
    epoch: Optional[int] = 1233,
    leaving_ids: Optional[set] = None,
    leaving: Optional[List[Dict[str, Any]]] = None,
    in_delay_period: bool = False,
    entering_ids: Optional[set] = None,
    entering: Optional[List[Dict[str, Any]]] = None,
) -> ValidatorSetState:
    """Build a ValidatorSetState shaped like the Huginn staking API response"""
    if leaving_ids is None:
        leaving_ids = {67}
    if leaving is None:
        leaving = [{"validator_id": 67, "name": "Unity Nodes", "stake": 11000000}]
    if entering_ids is None:
        entering_ids = {231}
    if entering is None:
        entering = [{"validator_id": 231, "name": "Example", "stake": 9000000}]
    return ValidatorSetState(
        network="testnet",
        epoch=epoch,
        in_delay_period=in_delay_period,
        counts={"leaving": len(leaving_ids), "entering": len(entering_ids)},
        leaving_ids=leaving_ids,
        leaving=leaving,
        entering_ids=entering_ids,
        entering=entering,
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
        self.is_active: Optional[bool] = True
        self.alerts = MagicMock()
        self.alerts.alert_warning.return_value = True
        self.client = MagicMock()
        self.client.get_validator_id.return_value = 67

    def _warn(self, enabled: bool = True, validator_set=None, is_active=None, boundary=None):
        return warn_if_leaving_next_epoch(
            enabled=enabled,
            validator=self.validator,
            state=self.state,
            is_active=self.is_active if is_active is None else is_active,
            validator_set=validator_set or make_validator_set(),
            next_epoch_boundary=boundary,
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
        assert "in the active set right now" in message
        assert "validator id 67" in message
        assert "stake 11000000" in message
        assert "next epoch boundary" in message
        # No clock time when no boundary was supplied - the ETA is optional
        assert "(local time)" not in message
        # The pending exit is remembered so it is not announced twice
        assert self.state["last_validator_set_warning_epoch"] == 1233
        # WARNING channel set only - Pushover is never used for M4
        self.alerts.send_pushover.assert_not_called()

    def test_warning_not_repeated_in_same_epoch(self):
        self._warn()
        second = self._warn()

        assert second is False
        self.alerts.alert_warning.assert_called_once()
        assert self.state["last_validator_set_warning_epoch"] == 1233

    def test_warning_not_repeated_when_huginn_advances_the_epoch(self):
        """One pending exit spans epochs: the label moving on must not re-alert."""
        self._warn()

        next_epoch_set = make_validator_set(
            epoch=1234,
            leaving_ids={67},
            leaving=[{"validator_id": 67, "name": "Unity Nodes", "stake": 11000000}],
        )
        second = self._warn(validator_set=next_epoch_set)

        assert second is False
        self.alerts.alert_warning.assert_called_once()
        assert self.state["last_validator_set_warning_epoch"] == 1233

    def test_warning_sent_again_for_a_later_exit(self):
        """Once the exit is off the leaving list, the next one alerts afresh."""
        self._warn()

        cleared = make_validator_set(
            leaving_ids={68},
            leaving=[{"validator_id": 68, "name": "Other", "stake": 5000000}],
        )
        self._warn(validator_set=cleared)
        assert self.state["last_validator_set_warning_epoch"] is None

        second = self._warn(validator_set=make_validator_set(epoch=1240))

        assert second is True
        assert self.alerts.alert_warning.call_count == 2
        assert self.state["last_validator_set_warning_epoch"] == 1240

    def test_warning_not_repeated_when_the_verdict_flaps_inactive(self):
        """A transient False verdict must not replay an exit that is still scheduled."""
        self._warn()

        flapped = self._warn(is_active=False)
        back = self._warn(is_active=True)

        assert flapped is False
        assert back is False
        self.alerts.alert_warning.assert_called_once()
        assert self.state["last_validator_set_warning_epoch"] == 1233

    def test_marker_kept_when_validator_set_is_unavailable(self):
        """With no leaving list there is no evidence the exit is over - hold."""
        self._warn()

        missing = warn_if_leaving_next_epoch(
            enabled=True,
            validator=self.validator,
            state=self.state,
            is_active=False,
            validator_set=None,
            huginn_client=self.client,
            alerts=self.alerts,
        )
        later = self._warn(is_active=True)

        assert missing is False
        assert later is False
        self.alerts.alert_warning.assert_called_once()
        assert self.state["last_validator_set_warning_epoch"] == 1233

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
        result = self._warn(is_active=False)

        assert result is False
        self.alerts.alert_warning.assert_not_called()

    def test_no_warning_when_verdict_unknown(self):
        result = warn_if_leaving_next_epoch(
            enabled=True,
            validator=self.validator,
            state=self.state,
            is_active=None,
            validator_set=make_validator_set(),
            huginn_client=self.client,
            alerts=self.alerts,
        )

        assert result is False
        self.alerts.alert_warning.assert_not_called()

    def test_warning_fires_when_only_gmonads_reports_active(self):
        """Combined verdict is authoritative: Huginn raw value no longer gates M4."""
        result = self._warn(is_active=True)

        assert result is True
        self.alerts.alert_warning.assert_called_once()

    def test_no_warning_when_validator_set_unavailable(self):
        result = warn_if_leaving_next_epoch(
            enabled=True,
            validator=self.validator,
            state=self.state,
            is_active=self.is_active,
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

    def test_testnet_message_notes_batch_rotation(self):
        result = self._warn()

        assert result is True
        message = self.alerts.alert_warning.call_args[0][0]
        assert "rotation script" in message
        assert "routine" in message
        assert "reverses in a later epoch" in message

    def test_mainnet_message_has_no_rotation_note(self):
        self.validator = ValidatorConfig(
            name="validator-main",
            host="192.168.1.100",
            metrics_port=8889,
            rpc_port=8080,
            node_exporter_port=None,
            validator_secp="0x1234",
            enabled=True,
            network="mainnet",
        )

        result = self._warn()

        assert result is True
        message = self.alerts.alert_warning.call_args[0][0]
        assert "rotation script" not in message

    def test_eta_is_named_when_the_boundary_is_known(self):
        result = self._warn(boundary=time.time() + 15240)

        assert result is True
        message = self.alerts.alert_warning.call_args[0][0]
        assert "next epoch boundary - in about 4h" in message
        assert "around" in message
        assert "(local time)" in message

    def test_eta_under_an_hour_drops_the_hour_part(self):
        self._warn(boundary=time.time() + 601)

        message = self.alerts.alert_warning.call_args[0][0]
        assert "in about 10m" in message

    def test_eta_is_omitted_during_a_delay_period(self):
        """A delay period pushes the transition, so a clock time would be wrong."""
        delay_set = make_validator_set(in_delay_period=True)

        result = self._warn(validator_set=delay_set, boundary=time.time() + 600)

        assert result is True
        message = self.alerts.alert_warning.call_args[0][0]
        assert "(local time)" not in message
        assert "one further epoch" in message

    def test_eta_is_omitted_when_the_boundary_is_unknown(self):
        result = self._warn(boundary=None)

        assert result is True
        message = self.alerts.alert_warning.call_args[0][0]
        assert "(local time)" not in message
        assert "next epoch boundary." in message

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
                is_active=self.is_active,
                validator_set=make_validator_set(),
                huginn_client=self.client,
                alerts=alerts,
            )

            assert result is True
            assert len(rsps.calls) == 1
            assert "api.pushover.net" not in rsps.calls[0].request.url
            assert "Leaving Active Set Next Epoch" in str(rsps.calls[0].request.body)


class TestReentryNotice:
    """Unit tests for notify_if_entering_next_epoch (mirror of the exit warning)"""

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
        self.state: Dict[str, Any] = {"last_validator_set_entry_notice_epoch": None}
        self.is_active: Optional[bool] = False
        self.alerts = MagicMock()
        self.alerts.alert_info.return_value = True
        self.client = MagicMock()
        self.client.get_validator_id.return_value = 231

    def _notify(self, enabled: bool = True, validator_set=None, is_active=None, boundary=None):
        return notify_if_entering_next_epoch(
            enabled=enabled,
            validator=self.validator,
            state=self.state,
            is_active=self.is_active if is_active is None else is_active,
            validator_set=validator_set or make_validator_set(),
            next_epoch_boundary=boundary,
            huginn_client=self.client,
            alerts=self.alerts,
        )

    def test_notice_sent_when_queued_to_re_enter(self):
        result = self._notify()

        assert result is True
        self.alerts.alert_info.assert_called_once()
        message = self.alerts.alert_info.call_args[0][0]
        assert "Entering the Active Set Next Epoch" in message
        assert "not in the active set right now" in message
        assert "validator id 231" in message
        assert "next epoch boundary" in message
        assert "(local time)" not in message
        assert self.state["last_validator_set_entry_notice_epoch"] == 1233
        # Good news travels as INFO; the exit side owns the WARNING
        self.alerts.alert_warning.assert_not_called()
        self.alerts.send_pushover.assert_not_called()

    def test_notice_not_repeated_when_the_epoch_advances(self):
        self._notify()

        second = self._notify(validator_set=make_validator_set(epoch=1234))

        assert second is False
        self.alerts.alert_info.assert_called_once()
        assert self.state["last_validator_set_entry_notice_epoch"] == 1233

    def test_notice_sent_again_for_a_later_re_entry(self):
        self._notify()

        self._notify(validator_set=make_validator_set(entering_ids=set(), entering=[]))
        assert self.state["last_validator_set_entry_notice_epoch"] is None

        second = self._notify(validator_set=make_validator_set(epoch=1240))

        assert second is True
        assert self.alerts.alert_info.call_count == 2

    def test_marker_from_a_left_alert_suppresses_the_notice(self):
        """A LEFT alert that already carries the return must not be repeated."""
        self.state["last_validator_set_entry_notice_epoch"] = 1233

        assert self._notify() is False
        self.alerts.alert_info.assert_not_called()

    def test_no_notice_while_still_active(self):
        assert self._notify(is_active=True) is False
        self.alerts.alert_info.assert_not_called()

    def test_no_notice_when_verdict_unknown(self):
        result = notify_if_entering_next_epoch(
            enabled=True,
            validator=self.validator,
            state=self.state,
            is_active=None,
            validator_set=make_validator_set(),
            next_epoch_boundary=None,
            huginn_client=self.client,
            alerts=self.alerts,
        )

        assert result is False
        self.alerts.alert_info.assert_not_called()

    def test_no_notice_when_disabled(self):
        assert self._notify(enabled=False) is False
        self.alerts.alert_info.assert_not_called()

    def test_no_notice_when_not_in_entering_list(self):
        result = self._notify(validator_set=make_validator_set(entering_ids=set(), entering=[]))

        assert result is False
        self.alerts.alert_info.assert_not_called()

    def test_no_notice_when_validator_set_unavailable(self):
        result = notify_if_entering_next_epoch(
            enabled=True,
            validator=self.validator,
            state=self.state,
            is_active=False,
            validator_set=None,
            next_epoch_boundary=None,
            huginn_client=self.client,
            alerts=self.alerts,
        )

        assert result is False
        self.alerts.alert_info.assert_not_called()

    def test_eta_named_when_the_boundary_is_known(self):
        self._notify(boundary=time.time() + 15240)

        message = self.alerts.alert_info.call_args[0][0]
        assert "next epoch boundary - in about 4h" in message
        assert "(local time)" in message

    def test_eta_omitted_during_a_delay_period(self):
        self._notify(
            validator_set=make_validator_set(in_delay_period=True),
            boundary=time.time() + 600,
        )

        message = self.alerts.alert_info.call_args[0][0]
        assert "(local time)" not in message
        assert "one further epoch" in message
