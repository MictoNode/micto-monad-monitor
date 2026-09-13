"""Channel-down warning (B4)

A channel can fail forever while the pipeline still looks healthy (the other
channels carry every alert). After a few consecutive outage-class failures the
monitor says so - once per episode, through the channels that still work.
"""

import json
import logging

import responses

from monad_monitor.alerts import (
    CHANNEL_FAILURE_ALERT_THRESHOLD,
    AlertHandler,
)

TELEGRAM_URL = "https://api.telegram.org/bottest-token/sendMessage"
DISCORD_URL = "https://discord.com/api/webhooks/1/hook"


def make_handler(telegram: bool = True, discord: bool = True) -> AlertHandler:
    return AlertHandler(
        telegram_token="test-token" if telegram else None,
        telegram_chat_id="chat" if telegram else None,
        discord_webhook_url=DISCORD_URL if discord else None,
    )


def fail_telegram(handler: AlertHandler) -> bool:
    """One Telegram send that fails with a server error (outage class)"""
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, TELEGRAM_URL, json={}, status=500)
        return handler.send_telegram("hello")


class TestChannelDownWarning:
    def test_threshold_crossing_warns_once_through_the_healthy_channel(self):
        handler = make_handler()

        for _ in range(CHANNEL_FAILURE_ALERT_THRESHOLD - 1):
            assert fail_telegram(handler) is False
        assert (
            handler.get_channel_stats()["telegram"]["consecutive_failures"]
            == CHANNEL_FAILURE_ALERT_THRESHOLD - 1
        )

        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, TELEGRAM_URL, json={}, status=500)
            rsps.add(responses.POST, DISCORD_URL, json={}, status=204)
            assert handler.send_telegram("hello") is False
            discord_calls = [c for c in rsps.calls if c.request.url == DISCORD_URL]

        assert len(discord_calls) == 1
        body = json.loads(discord_calls[0].request.body)["embeds"][0]["description"]
        assert "degraded" in body
        assert "telegram" in body

        # The warning must not feed the counters it is derived from
        assert (
            handler.get_channel_stats()["telegram"]["consecutive_failures"]
            == CHANNEL_FAILURE_ALERT_THRESHOLD
        )
        assert handler.get_channel_stats()["discord"]["sent"] == 0

    def test_no_second_warning_while_the_channel_stays_down(self):
        handler = make_handler()
        for _ in range(CHANNEL_FAILURE_ALERT_THRESHOLD):
            fail_telegram(handler)

        warnings = 0
        for _ in range(3):
            with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
                rsps.add(responses.POST, TELEGRAM_URL, json={}, status=500)
                rsps.add(responses.POST, DISCORD_URL, json={}, status=204)
                handler.send_telegram("hello")
                warnings += len([c for c in rsps.calls if c.request.url == DISCORD_URL])

        assert warnings == 0

    def test_recovery_rearms_the_warning(self):
        handler = make_handler()
        for _ in range(CHANNEL_FAILURE_ALERT_THRESHOLD):
            fail_telegram(handler)

        # Telegram works again: the streak resets
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, TELEGRAM_URL, json={"ok": True}, status=200)
            assert handler.send_telegram("recovered") is True
        assert handler.get_channel_stats()["telegram"]["consecutive_failures"] == 0

        # A new outage episode warns again
        for _ in range(CHANNEL_FAILURE_ALERT_THRESHOLD - 1):
            fail_telegram(handler)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, TELEGRAM_URL, json={}, status=500)
            rsps.add(responses.POST, DISCORD_URL, json={}, status=204)
            handler.send_telegram("hello")
            assert len([c for c in rsps.calls if c.request.url == DISCORD_URL]) == 1

    def test_client_error_is_not_an_outage(self):
        """A 400 comes from our own payload, so it must not mark the channel down"""
        handler = make_handler()

        for _ in range(CHANNEL_FAILURE_ALERT_THRESHOLD + 1):
            with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
                rsps.add(responses.POST, TELEGRAM_URL, json={}, status=400)
                rsps.add(responses.POST, DISCORD_URL, json={}, status=204)
                assert handler.send_telegram("hello") is False
                assert [c for c in rsps.calls if c.request.url == DISCORD_URL] == []

        stats = handler.get_channel_stats()["telegram"]
        assert stats["consecutive_failures"] == 0
        assert stats["failed"] == CHANNEL_FAILURE_ALERT_THRESHOLD + 1

    def test_all_channels_down_logs_and_does_not_raise(self, caplog):
        handler = make_handler(discord=False)

        with caplog.at_level(logging.ERROR):
            for _ in range(CHANNEL_FAILURE_ALERT_THRESHOLD):
                assert fail_telegram(handler) is False

        assert any(
            "no healthy channel is left" in r.getMessage() for r in caplog.records
        )
        assert handler.get_channel_stats()["telegram"]["consecutive_failures"] == CHANNEL_FAILURE_ALERT_THRESHOLD

    def test_pushover_is_not_used_for_the_warning(self):
        """Pushover stays reserved for CRITICAL alerts"""
        handler = AlertHandler(
            telegram_token="test-token",
            telegram_chat_id="chat",
            pushover_user_key="user",
            pushover_app_token="app",
        )

        for _ in range(CHANNEL_FAILURE_ALERT_THRESHOLD):
            fail_telegram(handler)

        assert handler.get_channel_stats()["pushover"]["sent"] == 0
