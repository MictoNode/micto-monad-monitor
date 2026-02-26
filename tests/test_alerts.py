"""Tests for AlertHandler and alert functionality"""

import json

import pytest
import responses

from monad_monitor.alerts import AlertHandler


class TestAlertHandler:
    """Test cases for AlertHandler"""

    @pytest.fixture
    def handler(self):
        """Create AlertHandler for testing"""
        return AlertHandler(
            telegram_token="test-telegram-token",
            telegram_chat_id="test-chat-id",
            pushover_user_key="test-user-key",
            pushover_app_token="test-app-token",
        )

    def test_send_telegram_success(self, handler):
        """Test successful Telegram message send"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True, "result": {"message_id": 123}},
                status=200,
            )

            result = handler.send_telegram("Test message")

            assert result is True

    def test_send_telegram_no_credentials(self):
        """Test Telegram send with no credentials"""
        handler = AlertHandler(
            telegram_token=None,
            telegram_chat_id=None,
        )
        result = handler.send_telegram("Test message")

        assert result is False

    def test_send_telegram_api_failure(self, handler):
        """Test Telegram send handles API failure"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": False, "error_code": 400},
                status=400,
            )

            result = handler.send_telegram("Test message")
            assert result is False

    def test_send_pushover_success(self, handler):
        """Test successful Pushover message send"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1, "request": "abc123"},
                status=200,
            )

            result = handler.send_pushover(
                message="Test message",
                title="Test Title",
                priority=0,
            )

            assert result is True

    def test_send_pushover_no_credentials(self):
        """Test Pushover send with no credentials"""
        handler = AlertHandler(
            telegram_token="test",
            telegram_chat_id="test",
            pushover_user_key=None,
            pushover_app_token=None,
        )
        result = handler.send_pushover("Test message")

        assert result is False

    def test_send_pushover_emergency_priority(self, handler):
        """Test Pushover emergency priority includes retry/expire"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )

            result = handler.send_pushover(
                message="Emergency!",
                title="CRITICAL",
                priority=2,  # Emergency
            )

            assert result is True
            # Verify request was made
            assert len(rsps.calls) == 1

    def test_alert_warning_sends_telegram(self, handler):
        """Test alert_warning sends Telegram message"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )

            handler.alert_warning("Warning message")

            # Check that message contains WARNING
            request_body = rsps.calls[0].request.body
            assert "WARNING" in str(request_body) or "warning" in str(request_body).lower()

    def test_alert_critical_sends_both(self, handler):
        """Test alert_critical sends both Telegram and Pushover"""
        with responses.RequestsMock() as rsps:
            # Telegram
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            # Pushover
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )

            handler.alert_critical("Critical message")

            # Both endpoints should be called
            assert len(rsps.calls) == 2

    def test_alert_critical_pushover_emergency_priority(self, handler):
        """Test alert_critical uses emergency priority for Pushover"""
        with responses.RequestsMock() as rsps:
            # Telegram
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            # Pushover
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )

            handler.alert_critical("Critical message")

            # Check Pushover request has priority 2
            pushover_call = rsps.calls[1]
            body = json.loads(pushover_call.request.body)
            assert body.get("priority") == 2

    def test_pushover_emergency_has_correct_params(self, handler):
        """Test emergency priority has retry=30, expire=3600, sound=persistent"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )

            handler.send_pushover(
                message="Emergency!",
                title="CRITICAL",
                priority=2,
            )

            body = json.loads(rsps.calls[0].request.body)
            assert body.get("priority") == 2
            assert body.get("retry") == 30
            assert body.get("expire") == 3600

    def test_alert_critical_uses_persistent_sound(self, handler):
        """Test alert_critical uses persistent sound for emergency"""
        with responses.RequestsMock() as rsps:
            # Telegram
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            # Pushover
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )

            handler.alert_critical("Critical message")

            pushover_call = rsps.calls[1]
            body = json.loads(pushover_call.request.body)
            assert body.get("sound") == "persistent"
            assert body.get("priority") == 2
            assert body.get("retry") == 30
            assert body.get("expire") == 3600

    def test_alert_info_sends_telegram(self, handler):
        """Test alert_info sends Telegram message"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )

            handler.alert_info("Info message")

            request_body = rsps.calls[0].request.body
            assert "INFO" in str(request_body) or "info" in str(request_body).lower()


class TestAlertHandlerRateLimiting:
    """Test cases for alert rate limiting"""

    def test_rate_limiting_exists(self):
        """Verify rate limiting mechanism exists"""
        handler = AlertHandler(
            telegram_token="test",
            telegram_chat_id="test",
        )

        assert hasattr(handler, "telegram_token")
        # Rate limiter should exist
        assert hasattr(handler, "_telegram_limiter")
        assert hasattr(handler, "_pushover_limiter")

    def test_rate_limiting_prevents_spam(self):
        """Test that rate limiting prevents excessive alerts"""
        handler = AlertHandler(
            telegram_token="test",
            telegram_chat_id="test",
            telegram_rate_limit=3,  # Low limit for testing
        )

        # First 3 should succeed
        for i in range(3):
            assert handler._telegram_limiter.can_consume() is True

        # Consume them
        for i in range(3):
            handler._telegram_limiter.consume(1)

        # 4th should be blocked
        assert handler._telegram_limiter.can_consume() is False


class TestCriticalAlertBypass:
    """Test cases for CRITICAL alert rate limit bypass (Season 4)"""

    @pytest.fixture
    def handler(self):
        """Create AlertHandler for testing"""
        return AlertHandler(
            telegram_token="test-telegram-token",
            telegram_chat_id="test-chat-id",
            pushover_user_key="test-user-key",
            pushover_app_token="test-app-token",
            telegram_rate_limit=2,  # Low limit for testing
            pushover_rate_limit=1,  # Low limit for testing
        )

    def test_critical_alert_bypasses_rate_limit(self, handler):
        """Test that CRITICAL alerts bypass rate limiting"""
        with responses.RequestsMock() as rsps:
            # Mock Telegram
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            # Mock Pushover
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )

            # Exhaust rate limiters
            for i in range(5):
                handler._telegram_limiter.consume(1)
                handler._pushover_limiter.consume(1)

            # Normal alert should be blocked
            assert handler.send_telegram("Normal message") is False

            # CRITICAL alert should still go through
            result = handler.alert_critical("Critical message")
            assert result is True
            assert len(rsps.calls) == 2  # Both Telegram and Pushover

    def test_alert_critical_returns_true_on_success(self, handler):
        """Test that alert_critical returns True when sent successfully"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )

            result = handler.alert_critical("Test critical")
            assert result is True

    def test_alert_critical_returns_false_on_all_failures(self, handler):
        """Test that alert_critical returns False when all channels fail"""
        with responses.RequestsMock() as rsps:
            # Both endpoints fail
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": False},
                status=500,
            )
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 0},
                status=500,
            )

            result = handler.alert_critical("Test critical")
            assert result is False

    def test_alert_critical_returns_true_if_one_channel_succeeds(self, handler):
        """Test that alert_critical returns True if at least one channel works"""
        with responses.RequestsMock() as rsps:
            # Telegram fails
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": False},
                status=500,
            )
            # Pushover succeeds
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )

            result = handler.alert_critical("Test critical")
            assert result is True

    def test_get_critical_stats(self, handler):
        """Test critical alert statistics tracking"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )

            # Send a critical alert
            handler.alert_critical("Test")

            stats = handler.get_critical_stats()
            assert stats["critical_alerts_sent"] == 1
            assert stats["critical_alerts_dropped"] == 0

    def test_warning_alert_is_rate_limited(self, handler):
        """Test that WARNING alerts are still rate limited"""
        # Exhaust rate limiter
        for i in range(5):
            handler._telegram_limiter.consume(1)

        # Warning should be blocked (no mock needed since request won't be made)
        result = handler.alert_warning("Warning message")
        assert result is False

    def test_send_telegram_bypass_flag(self, handler):
        """Test bypass_rate_limit flag in send_telegram"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )

            # Exhaust rate limiter
            for i in range(5):
                handler._telegram_limiter.consume(1)

            # Normal call should fail
            result = handler.send_telegram("Normal")
            assert result is False

            # Bypass should succeed
            result = handler.send_telegram("Bypassed", bypass_rate_limit=True)
            assert result is True

    def test_send_pushover_bypass_flag(self, handler):
        """Test bypass_rate_limit flag in send_pushover"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )

            # Exhaust rate limiter
            for i in range(5):
                handler._pushover_limiter.consume(1)

            # Normal call should fail
            result = handler.send_pushover("Normal")
            assert result is False

            # Bypass should succeed
            result = handler.send_pushover("Bypassed", bypass_rate_limit=True)
            assert result is True


class TestFailedAlertRetry:
    """Test cases for failed alert retry queue (Session 22)"""

    @pytest.fixture
    def handler(self):
        """Create AlertHandler for testing"""
        return AlertHandler(
            telegram_token="test-telegram-token",
            telegram_chat_id="test-chat-id",
            pushover_user_key="test-user-key",
            pushover_app_token="test-app-token",
        )

    def test_failed_alert_is_queued(self, handler):
        """Test that failed critical alerts are queued for retry"""
        with responses.RequestsMock() as rsps:
            # Both endpoints fail
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": False},
                status=500,
            )
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 0},
                status=500,
            )

            result = handler.alert_critical("Test critical", validator_name="TestValidator")
            assert result is False
            assert handler.get_failed_queue_size() == 1

    def test_retry_failed_alerts_succeeds(self, handler):
        """Test that retry_failed_alerts sends queued alerts"""
        with responses.RequestsMock() as rsps:
            # First call fails
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": False},
                status=500,
            )
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 0},
                status=500,
            )

            handler.alert_critical("Test message", validator_name="TestValidator")
            assert handler.get_failed_queue_size() == 1

        # Now mock success for retry
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )

            sent = handler.retry_failed_alerts()
            assert sent == 1
            assert handler.get_failed_queue_size() == 0

    def test_retry_persists_on_failure(self, handler):
        """Test that failed retries are re-queued"""
        with responses.RequestsMock() as rsps:
            # First call fails
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": False},
                status=500,
            )
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 0},
                status=500,
            )

            handler.alert_critical("Test message", validator_name="TestValidator")

        # Retry also fails
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": False},
                status=500,
            )
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 0},
                status=500,
            )

            sent = handler.retry_failed_alerts()
            assert sent == 0
            # Still in queue
            assert handler.get_failed_queue_size() == 1

    def test_queue_size_limit(self):
        """Test that queue has a maximum size"""
        handler = AlertHandler(
            telegram_token="test",
            telegram_chat_id="test",
        )

        # Directly fill queue to max
        from monad_monitor.alerts import MAX_FAILED_ALERTS_QUEUE_SIZE
        for i in range(MAX_FAILED_ALERTS_QUEUE_SIZE):
            handler._failed_alerts_queue.append((f"msg{i}", f"val{i}", 0))

        # Queue is at max
        assert len(handler._failed_alerts_queue) == MAX_FAILED_ALERTS_QUEUE_SIZE

        # Now trigger _queue_failed_alert which should drop oldest to make room
        handler._queue_failed_alert("new msg", "new val")

        # Should have dropped oldest to stay at limit
        assert len(handler._failed_alerts_queue) == MAX_FAILED_ALERTS_QUEUE_SIZE
        # Oldest should be gone
        assert handler._failed_alerts_queue[0][0] == "msg1"  # msg0 was dropped
        # Newest should be present
        assert handler._failed_alerts_queue[-1][0] == "new msg"

    def test_successful_alert_not_queued(self, handler):
        """Test that successful alerts are not queued"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )

            result = handler.alert_critical("Test critical", validator_name="TestValidator")
            assert result is True
            assert handler.get_failed_queue_size() == 0


class TestDiscordWebhook:
    """Test cases for Discord webhook integration (Session 22)"""

    @pytest.fixture
    def handler_with_discord(self):
        """Create AlertHandler with Discord configured"""
        return AlertHandler(
            telegram_token="test-telegram-token",
            telegram_chat_id="test-chat-id",
            pushover_user_key="test-user-key",
            pushover_app_token="test-app-token",
            discord_webhook_url="https://discord.com/api/webhooks/123/abc",
        )

    @pytest.fixture
    def handler_no_discord(self):
        """Create AlertHandler without Discord"""
        return AlertHandler(
            telegram_token="test-telegram-token",
            telegram_chat_id="test-chat-id",
        )

    def test_send_discord_success(self, handler_with_discord):
        """Test successful Discord message send"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://discord.com/api/webhooks/123/abc",
                body="",  # Empty body for 204-style response
                status=204,
            )

            result = handler_with_discord.send_discord("Test message")
            assert result is True

    def test_send_discord_no_webhook(self, handler_no_discord):
        """Test Discord send with no webhook configured"""
        result = handler_no_discord.send_discord("Test message")
        assert result is False

    def test_send_discord_api_failure(self, handler_with_discord):
        """Test Discord send handles API failure"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://discord.com/api/webhooks/123/abc",
                json={"message": "Invalid Webhook"},
                status=404,
            )

            result = handler_with_discord.send_discord("Test message")
            assert result is False

    def test_discord_embed_format(self, handler_with_discord):
        """Test that Discord message uses embed format"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://discord.com/api/webhooks/123/abc",
                body="",
                status=204,
            )

            handler_with_discord.send_discord("Test message", title="Test Title", color=0xFF0000)

            request_body = json.loads(rsps.calls[0].request.body)
            assert "embeds" in request_body
            assert len(request_body["embeds"]) == 1
            assert request_body["embeds"][0]["title"] == "Test Title"
            assert request_body["embeds"][0]["description"] == "Test message"
            assert request_body["embeds"][0]["color"] == 0xFF0000

    def test_critical_alert_sends_to_discord(self, handler_with_discord):
        """Test that alert_critical sends to Discord"""
        with responses.RequestsMock() as rsps:
            # Telegram
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": True},
                status=200,
            )
            # Pushover
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 1},
                status=200,
            )
            # Discord
            rsps.add(
                responses.POST,
                "https://discord.com/api/webhooks/123/abc",
                body="",
                status=204,
            )

            result = handler_with_discord.alert_critical("Critical message")
            assert result is True
            # Should have 3 calls (Telegram, Pushover, Discord)
            assert len(rsps.calls) == 3

    def test_discord_rate_limiting(self, handler_with_discord):
        """Test that Discord is rate limited for non-critical alerts"""
        # Exhaust rate limiter
        for i in range(10):
            handler_with_discord._discord_limiter.consume(1)

        # Should be blocked
        result = handler_with_discord.send_discord("Normal message")
        assert result is False

    def test_discord_bypass_rate_limit(self, handler_with_discord):
        """Test that Discord bypasses rate limit for critical alerts"""
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "https://discord.com/api/webhooks/123/abc",
                body="",
                status=204,
            )

            # Exhaust rate limiter
            for i in range(10):
                handler_with_discord._discord_limiter.consume(1)

            # With bypass should succeed
            result = handler_with_discord.send_discord("Critical", bypass_rate_limit=True)
            assert result is True

    def test_critical_success_with_only_discord(self, handler_with_discord):
        """Test that critical returns True if only Discord succeeds"""
        with responses.RequestsMock() as rsps:
            # Telegram fails
            rsps.add(
                responses.POST,
                "https://api.telegram.org/bottest-telegram-token/sendMessage",
                json={"ok": False},
                status=500,
            )
            # Pushover fails
            rsps.add(
                responses.POST,
                "https://api.pushover.net/1/messages.json",
                json={"status": 0},
                status=500,
            )
            # Discord succeeds
            rsps.add(
                responses.POST,
                "https://discord.com/api/webhooks/123/abc",
                body="",
                status=204,
            )

            result = handler_with_discord.alert_critical("Critical message")
            assert result is True
