"""Alert handlers - Telegram, Pushover, and Discord"""

import time
from typing import Dict, Optional, List, Tuple

import requests

from .rate_limiter import TokenBucketRateLimiter
from .logger import get_logger

logger = get_logger()

# Default cooldown period for Pushover CRITICAL alerts (30 minutes)
# This prevents alert storms during network-wide issues
PUSHOVER_CRITICAL_COOLDOWN_SECONDS = 30 * 60

# Maximum number of failed alerts to queue for retry
MAX_FAILED_ALERTS_QUEUE_SIZE = 10


class AlertHandler:
    """Handle alerts via Telegram, Pushover, and Discord with rate limiting

    Rate Limiting Strategy:
    - WARNING/INFO: Subject to rate limiting (prevents spam)
    - CRITICAL Telegram: BYPASSES rate limit (never miss critical alerts)
    - CRITICAL Pushover: Has 30-minute cooldown to prevent alert storms
    - Discord: Rate limited for non-critical, bypassed for critical
    """

    TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
    PUSHOVER_API = "https://api.pushover.net/1/messages.json"

    def __init__(
        self,
        telegram_token: str,
        telegram_chat_id: str,
        pushover_user_key: Optional[str] = None,
        pushover_app_token: Optional[str] = None,
        discord_webhook_url: Optional[str] = None,
        telegram_rate_limit: int = 10,  # Max 10 alerts per minute
        pushover_rate_limit: int = 5,  # Max 5 alerts per minute
        discord_rate_limit: int = 5,  # Max 5 alerts per minute
        pushover_critical_cooldown: int = PUSHOVER_CRITICAL_COOLDOWN_SECONDS,
    ):
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.pushover_user_key = pushover_user_key
        self.pushover_app_token = pushover_app_token
        self.discord_webhook_url = discord_webhook_url
        self.pushover_critical_cooldown = pushover_critical_cooldown

        # Initialize rate limiters
        self._telegram_limiter = TokenBucketRateLimiter(
            max_tokens=telegram_rate_limit,
            refill_rate=telegram_rate_limit / 60.0  # Refill to full capacity over 1 minute
        )
        self._pushover_limiter = TokenBucketRateLimiter(
            max_tokens=pushover_rate_limit,
            refill_rate=pushover_rate_limit / 60.0
        )
        self._discord_limiter = TokenBucketRateLimiter(
            max_tokens=discord_rate_limit,
            refill_rate=discord_rate_limit / 60.0
        )

        # Track critical alerts sent (for monitoring)
        self._critical_alerts_sent = 0
        self._critical_alerts_dropped = 0

        # Track last Pushover CRITICAL alert time per validator (for cooldown)
        # Key: validator_name, Value: timestamp of last Pushover CRITICAL
        self._pushover_critical_last_sent: Dict[str, float] = {}

        # Failed alerts queue for retry (prevents alert loss on network issues)
        # Each entry: (message, validator_name, timestamp_failed)
        self._failed_alerts_queue: List[Tuple[str, Optional[str], float]] = []

    def send_telegram(
        self,
        message: str,
        parse_mode: str = "Markdown",
        bypass_rate_limit: bool = False,
    ) -> bool:
        """Send message via Telegram bot with rate limiting

        Args:
            message: Message to send
            parse_mode: Telegram parse mode (default: Markdown)
            bypass_rate_limit: If True, skip rate limiting (for CRITICAL alerts)

        Returns:
            True if message was sent successfully, False otherwise
        """
        if not self.telegram_token or not self.telegram_chat_id:
            logger.warning("Telegram credentials not configured")
            return False

        # Check rate limit (unless bypassed for critical alerts)
        if not bypass_rate_limit:
            if not self._telegram_limiter.consume(1):
                logger.warning("Telegram rate limit exceeded - message dropped")
                return False

        url = self.TELEGRAM_API.format(token=self.telegram_token)
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": message,
            "parse_mode": parse_mode,
        }

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            if bypass_rate_limit:
                logger.info("Telegram CRITICAL alert sent (rate limit bypassed)")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Telegram send error: {e}")
            return False

    def send_pushover(
        self,
        message: str,
        title: str = "Monad Alert",
        priority: int = 0,
        sound: str = "pushover",
        bypass_rate_limit: bool = False,
        validator_name: Optional[str] = None,
    ) -> bool:
        """Send message via Pushover (emergency alerts that bypass DND) with rate limiting

        Args:
            message: Message to send
            title: Alert title
            priority: Pushover priority (0=normal, 1=high, 2=emergency)
            sound: Notification sound
            bypass_rate_limit: If True, skip rate limiting (for CRITICAL alerts)
            validator_name: Optional validator name for cooldown tracking

        Returns:
            True if message was sent successfully, False otherwise
        """
        if not self.pushover_user_key or not self.pushover_app_token:
            logger.debug("Pushover credentials not configured - skipping Pushover alert")
            return False

        # For CRITICAL alerts (priority 2), check cooldown
        if priority == 2 and validator_name:
            now = time.time()
            last_sent = self._pushover_critical_last_sent.get(validator_name, 0)
            time_since_last = now - last_sent

            if time_since_last < self.pushover_critical_cooldown:
                remaining = int(self.pushover_critical_cooldown - time_since_last)
                logger.info(
                    f"Pushover CRITICAL for {validator_name} in cooldown "
                    f"({remaining}s remaining) - alert suppressed"
                )
                return False  # Cooldown active, suppress this alert

        # Check rate limit (unless bypassed for critical alerts)
        # Emergency alerts use more tokens normally, but bypass if critical
        if not bypass_rate_limit:
            tokens_needed = 2 if priority == 2 else 1
            if not self._pushover_limiter.consume(tokens_needed):
                logger.warning("Pushover rate limit exceeded - message dropped")
                return False

        payload = {
            "user": self.pushover_user_key,
            "token": self.pushover_app_token,
            "message": message,
            "title": title,
            "priority": priority,
            "sound": sound,
        }

        # Emergency priority (2) requires retry and expire
        if priority == 2:
            payload["retry"] = 30  # Retry every 30 seconds
            payload["expire"] = 3600  # Keep retrying for 1 hour

        try:
            response = requests.post(self.PUSHOVER_API, json=payload, timeout=10)
            response.raise_for_status()

            # Update cooldown tracker for successful CRITICAL alerts
            if priority == 2 and validator_name:
                self._pushover_critical_last_sent[validator_name] = time.time()

            if bypass_rate_limit:
                logger.info(f"Pushover CRITICAL alert sent for {validator_name or 'unknown'}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Pushover send error: {e}")
            return False

    def send_discord(
        self,
        message: str,
        title: str = "Monad Alert",
        color: int = 0x3498db,  # Blue default
        bypass_rate_limit: bool = False,
    ) -> bool:
        """Send message via Discord webhook with rate limiting

        Args:
            message: Message to send
            title: Embed title
            color: Embed color (hex int, default blue)
            bypass_rate_limit: If True, skip rate limiting (for CRITICAL alerts)

        Returns:
            True if message was sent successfully, False otherwise
        """
        if not self.discord_webhook_url:
            logger.debug("Discord webhook not configured - skipping Discord alert")
            return False

        # Check rate limit (unless bypassed for critical alerts)
        if not bypass_rate_limit:
            if not self._discord_limiter.consume(1):
                logger.warning("Discord rate limit exceeded - message dropped")
                return False

        # Discord embed format for better readability
        payload = {
            "embeds": [
                {
                    "title": title,
                    "description": message,
                    "color": color,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            ]
        }

        try:
            response = requests.post(self.discord_webhook_url, json=payload, timeout=10)
            response.raise_for_status()

            if bypass_rate_limit:
                logger.info("Discord CRITICAL alert sent (rate limit bypassed)")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Discord send error: {e}")
            return False

    def alert_warning(self, message: str) -> bool:
        """Send warning alert (Telegram + Discord, rate limited)

        Returns:
            True if sent successfully to at least one channel, False otherwise
        """
        telegram_success = self.send_telegram(f"⚠️ *WARNING*\n\n{message}")
        discord_success = self.send_discord(
            message=message,
            title="⚠️ MONAD WARNING",
            color=0xf39c12,  # Orange for warning
        )
        return telegram_success or discord_success

    def alert_critical(self, message: str, validator_name: Optional[str] = None) -> bool:
        """Send critical alert (Telegram + Pushover + Discord)

        Telegram: Bypasses rate limit (never miss critical alerts)
        Pushover: Has 30-minute cooldown per validator to prevent alert storms
        Discord: Bypasses rate limit for critical alerts

        Args:
            message: Alert message to send
            validator_name: Optional validator name for Pushover cooldown tracking

        Returns:
            True if at least one channel sent successfully, False otherwise
        """
        telegram_success = False
        pushover_success = False
        discord_success = False

        # Telegram alert (bypasses rate limit)
        telegram_success = self.send_telegram(
            f"🔴 *CRITICAL*\n\n{message}",
            bypass_rate_limit=True,
        )

        # Pushover emergency alert (has cooldown to prevent storms)
        if self.pushover_user_key and self.pushover_app_token:
            pushover_success = self.send_pushover(
                message=message,
                title="MONAD CRITICAL ALERT",
                priority=2,  # Emergency priority
                sound="persistent",  # Persistent sound for emergency
                bypass_rate_limit=True,
                validator_name=validator_name,
            )

        # Discord alert (bypasses rate limit for critical)
        discord_success = self.send_discord(
            message=message,
            title="🔴 MONAD CRITICAL ALERT",
            color=0xe74c3c,  # Red for critical
            bypass_rate_limit=True,
        )

        # Track for monitoring
        if telegram_success or pushover_success or discord_success:
            self._critical_alerts_sent += 1
            return True
        else:
            self._critical_alerts_dropped += 1
            logger.error("CRITICAL alert failed to send on ALL channels!")
            # Queue for retry to prevent alert loss
            self._queue_failed_alert(message, validator_name)
            return False

    def alert_info(self, message: str) -> bool:
        """Send info alert (Telegram + Discord, rate limited)

        Returns:
            True if sent successfully to at least one channel, False otherwise
        """
        telegram_success = self.send_telegram(f"ℹ️ *INFO*\n\n{message}")
        discord_success = self.send_discord(
            message=message,
            title="ℹ️ MONAD INFO",
            color=0x3498db,  # Blue for info
        )
        return telegram_success or discord_success

    def alert_network(self, message: str) -> bool:
        """Send network-wide alert (Telegram + Discord, rate limited)

        Returns:
            True if sent successfully to at least one channel, False otherwise
        """
        telegram_success = self.send_telegram(f"🌐 *NETWORK*\n\n{message}")
        discord_success = self.send_discord(
            message=message,
            title="🌐 MONAD NETWORK",
            color=0x9b59b6,  # Purple for network
        )
        return telegram_success or discord_success

    def get_critical_stats(self) -> dict:
        """Get statistics about critical alerts (for monitoring)"""
        return {
            "critical_alerts_sent": self._critical_alerts_sent,
            "critical_alerts_dropped": self._critical_alerts_dropped,
        }

    def reset_pushover_cooldown(self, validator_name: str) -> None:
        """Reset the Pushover CRITICAL cooldown for a validator.

        Call this when a validator recovers to ensure immediate alerts
        if it fails again.

        Args:
            validator_name: Name of the validator to reset cooldown for
        """
        if validator_name in self._pushover_critical_last_sent:
            del self._pushover_critical_last_sent[validator_name]
            logger.debug(f"Pushover cooldown reset for {validator_name}")

    def _queue_failed_alert(self, message: str, validator_name: Optional[str]) -> None:
        """Queue a failed alert for retry.

        Args:
            message: The alert message that failed to send
            validator_name: Optional validator name
        """
        if len(self._failed_alerts_queue) >= MAX_FAILED_ALERTS_QUEUE_SIZE:
            # Remove oldest entry to make room
            old_msg, old_val, _ = self._failed_alerts_queue.pop(0)
            logger.warning(f"Dropping oldest failed alert to make room: {old_val or 'unknown'}")

        self._failed_alerts_queue.append((message, validator_name, time.time()))
        logger.info(f"Queued failed alert for retry: {validator_name or 'unknown'} (queue size: {len(self._failed_alerts_queue)})")

    def retry_failed_alerts(self) -> int:
        """Retry sending all failed alerts in the queue.

        Call this periodically from the main loop to retry failed alerts.

        Returns:
            Number of alerts successfully sent
        """
        if not self._failed_alerts_queue:
            return 0

        sent_count = 0
        retry_queue = self._failed_alerts_queue.copy()
        self._failed_alerts_queue.clear()

        for message, validator_name, failed_at in retry_queue:
            age_seconds = int(time.time() - failed_at)
            logger.info(f"Retrying failed alert for {validator_name or 'unknown'} (age: {age_seconds}s)")

            # Try to send again
            telegram_success = self.send_telegram(
                f"🔴 *CRITICAL* (Retry)\n\n{message}",
                bypass_rate_limit=True,
            )

            pushover_success = False
            if self.pushover_user_key and self.pushover_app_token:
                pushover_success = self.send_pushover(
                    message=f"[RETRY] {message}",
                    title="MONAD CRITICAL ALERT (Retry)",
                    priority=2,
                    sound="persistent",
                    bypass_rate_limit=True,
                    validator_name=validator_name,
                )

            discord_success = self.send_discord(
                message=f"[RETRY] {message}",
                title="🔴 MONAD CRITICAL ALERT (Retry)",
                color=0xe74c3c,  # Red for critical
                bypass_rate_limit=True,
            )

            if telegram_success or pushover_success or discord_success:
                sent_count += 1
                logger.info(f"Successfully retried alert for {validator_name or 'unknown'}")
            else:
                # Still failing, re-queue if not too old (max 1 hour)
                if age_seconds < 3600:
                    self._failed_alerts_queue.append((message, validator_name, failed_at))
                    logger.warning(f"Retry failed for {validator_name or 'unknown'}, re-queued")
                else:
                    logger.error(f"Dropping stale alert for {validator_name or 'unknown'} (age: {age_seconds}s)")

        return sent_count

    def get_failed_queue_size(self) -> int:
        """Get the number of failed alerts waiting for retry."""
        return len(self._failed_alerts_queue)
