"""Tests for handle_huginn_timeout (main.py)

Huginn's cumulative timeout counter is the only network-side view of missed
rounds. The baseline advances only when the alert actually went out, so a moment
where every channel is down cannot swallow the alert for that increase.
"""

from typing import Any, Dict, Optional
from unittest.mock import MagicMock

from monad_monitor.main import handle_huginn_timeout


def make_state(last: Optional[int] = None) -> Dict[str, Any]:
    return {"last_huginn_timeout_count": last}


class TestHuginnTimeoutHandler:
    def setup_method(self):
        self.alerts = MagicMock()
        self.alerts.alert_critical.return_value = True

    def _handle(self, state: Dict[str, Any], count: int, threshold: int = 1) -> bool:
        return handle_huginn_timeout(
            state=state,
            validator_name="validator-1",
            huginn_data={"timeout_count": count},
            threshold=threshold,
            alerts=self.alerts,
        )

    def test_first_check_only_records_the_baseline(self):
        state = make_state(None)

        assert self._handle(state, 57) is False
        self.alerts.alert_critical.assert_not_called()
        assert state["last_huginn_timeout_count"] == 57

    def test_increase_alerts_and_advances_the_baseline(self):
        state = make_state(57)

        assert self._handle(state, 58) is True

        self.alerts.alert_critical.assert_called_once()
        assert state["last_huginn_timeout_count"] == 58

    def test_failed_send_keeps_the_baseline_for_the_next_cycle(self):
        state = make_state(57)
        self.alerts.alert_critical.return_value = False

        assert self._handle(state, 60) is False
        assert state["last_huginn_timeout_count"] == 57

        # Next cycle offers the same increase again and this time it goes out
        self.alerts.alert_critical.return_value = True
        assert self._handle(state, 60) is True
        assert state["last_huginn_timeout_count"] == 60

    def test_increase_below_threshold_only_advances_the_baseline(self):
        state = make_state(57)

        assert self._handle(state, 58, threshold=3) is False

        self.alerts.alert_critical.assert_not_called()
        assert state["last_huginn_timeout_count"] == 58

    def test_flat_count_never_alerts(self):
        state = make_state(57)

        assert self._handle(state, 57) is False
        self.alerts.alert_critical.assert_not_called()
        assert state["last_huginn_timeout_count"] == 57

    def test_decreasing_count_never_alerts(self):
        state = make_state(60)

        assert self._handle(state, 2) is False
        self.alerts.alert_critical.assert_not_called()
        assert state["last_huginn_timeout_count"] == 2
