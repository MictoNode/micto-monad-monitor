"""Alert handlers - Telegram, Pushover, Discord, and Slack"""

import json
import os
import re
import threading
import time
from typing import Dict, Optional, List, Tuple, Union

import requests

from .rate_limiter import TokenBucketRateLimiter
from .logger import get_logger

logger = get_logger()

# Default cooldown period for Pushover CRITICAL alerts (30 minutes)
# This prevents alert storms during network-wide issues
PUSHOVER_CRITICAL_COOLDOWN_SECONDS = 30 * 60

# Maximum number of failed alerts to queue for retry
MAX_FAILED_ALERTS_QUEUE_SIZE = 10

# Failed alerts older than this are dropped instead of retried (a stale
# "node down" from an hour ago is noise, not news)
FAILED_ALERT_MAX_AGE_SECONDS = 3600

# How many queued alerts are retried per monitoring cycle. Each retry fans out
# to four channels with a 10s timeout, so an unbounded batch can stall the loop
# for minutes during a channel outage; the rest waits for the next cycle (and
# survives a restart because the queue is persisted).
MAX_RETRY_PER_CYCLE = 3

# Consecutive outage-class failures on a channel before one WARNING is sent
# through the healthy channels
CHANNEL_FAILURE_ALERT_THRESHOLD = 3

# Credentials live inside the request URL (Telegram bot token, Discord/Slack
# webhook ids) and `requests` echoes that URL back in exception text - including
# the scheme-less "url: /bot<TOKEN>/sendMessage" form urllib3 uses for
# ConnectionError. Every send failure is therefore redacted before it is logged.
_URL_IN_ERROR_RE = re.compile(r"(?:https?://|url:\s*)\S+")
CHANNELS = ("telegram", "pushover", "discord", "slack")

# Legacy Markdown (Telegram's parse_mode="Markdown") treats these as markup:
# [text](url), *bold*, _italic_, `code`. A validator name (or any API-provided
# string) containing one of them makes Telegram reject the whole message with
# 400 "can't parse entities" - i.e. the channel goes silent for that validator.
_MARKDOWN_ESCAPE_RE = re.compile(r"([_*`\[\]\\])")
_MARKDOWN_ESCAPED_RE = re.compile(r"\\([_*`\[\]\\])")


def escape_markdown(text: str) -> str:
    """Escape legacy-Markdown characters in a DYNAMIC value (name, host, ...).

    Static message text keeps its intentional markup; only values interpolated
    into it go through here.
    """
    return _MARKDOWN_ESCAPE_RE.sub(r"\\\1", str(text))


def unescape_markdown(text: str) -> str:
    """Undo escape_markdown for channels that render text verbatim.

    Discord treats a backslash as an escape, Slack does not, but neither should
    display the backslashes this module added - only our own escape sequences
    are undone, so an unrelated backslash in the text survives untouched.
    """
    return _MARKDOWN_ESCAPED_RE.sub(r"\1", text)


def _failure_is_outage(exc: Exception) -> bool:
    """Whether a send failure means the channel is down (vs our message bad).

    400/413 come from our own payload (unparseable/too long), so they must not
    mark a healthy channel as down; auth failures, rate limits, missing
    endpoints, server errors and connection errors all do.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return True  # connection error, timeout, DNS, ...
    status = getattr(response, "status_code", None)
    if status is None:
        return True
    if status in (400, 413):
        return False
    return True


def redact_error(text: str, secrets: Optional[List[str]] = None) -> str:
    """Strip URLs and known credentials from an exception message"""
    redacted = _URL_IN_ERROR_RE.sub("<redacted>", text)
    for secret in secrets or []:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


class AlertHandler:
    """Handle alerts via Telegram, Pushover, Discord, and Slack with rate limiting

    Rate Limiting Strategy:
    - WARNING/INFO: Subject to rate limiting (prevents spam)
    - CRITICAL Telegram: BYPASSES rate limit (never miss critical alerts)
    - CRITICAL Pushover: Has 30-minute cooldown to prevent alert storms
    - Discord/Slack: Rate limited for non-critical, bypassed for critical
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
        slack_webhook_url: Optional[str] = None,
        telegram_rate_limit: int = 10,  # Max 10 alerts per minute
        pushover_rate_limit: int = 5,  # Max 5 alerts per minute
        discord_rate_limit: int = 5,  # Max 5 alerts per minute
        slack_rate_limit: int = 5,  # Max 5 alerts per minute
        pushover_critical_cooldown: int = PUSHOVER_CRITICAL_COOLDOWN_SECONDS,
        failed_alerts_path: Optional[str] = None,
    ):
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.pushover_user_key = pushover_user_key
        self.pushover_app_token = pushover_app_token
        self.discord_webhook_url = discord_webhook_url
        self.slack_webhook_url = slack_webhook_url
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
        self._slack_limiter = TokenBucketRateLimiter(
            max_tokens=slack_rate_limit,
            refill_rate=slack_rate_limit / 60.0
        )

        # Track critical alerts sent (for monitoring)
        self._critical_alerts_sent = 0
        self._critical_alerts_dropped = 0

        # Track last Pushover CRITICAL alert time per validator (for cooldown)
        # Key: validator_name, Value: timestamp of last Pushover CRITICAL
        self._pushover_critical_last_sent: Dict[str, float] = {}

        # Per-channel delivery counters. Visibility only - the monitor does not
        # alert on them (a "channel down" alert needs its own design). Only
        # attempts that reach the network are counted: a missing credential or a
        # rate-limit drop says nothing about the channel's health.
        self._channel_stats: Dict[str, Dict[str, int]] = {
            channel: {"sent": 0, "failed": 0, "consecutive_failures": 0}
            for channel in CHANNELS
        }
        self._stats_lock = threading.Lock()

        # Credentials echoed back by exception text; never let them reach a log line
        self._secrets = [
            secret
            for secret in (
                telegram_token,
                pushover_app_token,
                pushover_user_key,
                discord_webhook_url,
                slack_webhook_url,
            )
            if secret
        ]

        # Failed alerts queue for retry (prevents alert loss on network issues)
        # Each entry: (message, validator_name, timestamp_failed)
        self._failed_alerts_queue: List[Tuple[str, Optional[str], float]] = []
        # Optional state-volume path: the queue survives a restart/crash there
        self._failed_alerts_path = failed_alerts_path
        self._queue_write_failed_logged = False
        self._load_failed_alerts()

        # A channel that crossed the outage threshold already had its operator
        # warning; reset when it delivers again (one warning per episode)
        self._channel_down_notified: Dict[str, bool] = {c: False for c in CHANNELS}

    def _record_send(
        self, channel: str, success: bool, track: bool = True, outage: bool = True
    ) -> bool:
        """Record one channel delivery outcome.

        Args:
            track: False for messages the monitor sends about itself (a
                channel-down warning), so they never perturb the counters they
                are derived from.
            outage: whether this failure means the channel is unreachable. Our
                own malformed/oversized payloads do not.

        Returns:
            True when this call crossed the channel-down threshold (the caller
            warns AFTER this returns, i.e. with the lock released).
        """
        if not track:
            return False

        with self._stats_lock:
            stats = self._channel_stats[channel]
            if success:
                stats["sent"] += 1
                stats["consecutive_failures"] = 0
                self._channel_down_notified[channel] = False
                return False

            stats["failed"] += 1
            if outage:
                stats["consecutive_failures"] += 1
            if (
                outage
                and stats["consecutive_failures"] >= CHANNEL_FAILURE_ALERT_THRESHOLD
                and not self._channel_down_notified[channel]
            ):
                self._channel_down_notified[channel] = True
                return True
            return False

    def get_channel_stats(self) -> Dict[str, Dict[str, int]]:
        """Per-channel delivery counters (copy - safe to serialize off-thread)"""
        with self._stats_lock:
            return {
                channel: dict(stats) for channel, stats in self._channel_stats.items()
            }

    def _channel_is_configured(self, channel: str) -> bool:
        """Whether a channel has credentials/webhook configured"""
        return {
            "telegram": bool(self.telegram_token and self.telegram_chat_id),
            "pushover": bool(self.pushover_user_key and self.pushover_app_token),
            "discord": bool(self.discord_webhook_url),
            "slack": bool(self.slack_webhook_url),
        }[channel]

    def _warn_channel_down(self, channel: str) -> bool:
        """Tell the operator, through the healthy channels, that one is down.

        Sent AFTER the stats lock is released, with track=False so the warning
        neither feeds the counters nor re-triggers itself. Pushover is excluded
        by construction (`alert_warning` never uses it). Returns True if any
        channel took the message.
        """
        stats = self.get_channel_stats()
        healthy = [
            other
            for other in CHANNELS
            if other != channel
            # WARNING-class message: Pushover stays reserved for CRITICAL alerts
            and other != "pushover"
            and self._channel_is_configured(other)
            and stats[other]["consecutive_failures"] < CHANNEL_FAILURE_ALERT_THRESHOLD
        ]
        if not healthy:
            logger.error(
                f"Alert channel '{channel}' is failing and no healthy channel is "
                f"left to report it - check /health 'alerts' counters"
            )
            return False

        message = (
            f"⚠️ Alert channel degraded: *{channel}*\n\n"
            f"{stats[channel]['consecutive_failures']} consecutive delivery failures "
            f"(configured={self._channel_is_configured(channel)}).\n"
            f"Alerts are still going out through: {', '.join(healthy)}.\n"
            f"Verify the {channel} credentials/webhook in your configuration."
        )
        delivered = False
        for other in healthy:
            if other == "telegram":
                delivered |= self.send_telegram(message, track=False)
            elif other == "discord":
                delivered |= self.send_discord(message, track=False)
            elif other == "slack":
                delivered |= self.send_slack(message, track=False)
            elif other == "pushover":
                delivered |= self.send_pushover(message, track=False)
        if delivered:
            logger.warning(
                f"Alert channel '{channel}' down after "
                f"{stats[channel]['consecutive_failures']} failures - warned "
                f"through {', '.join(healthy)}"
            )
        return delivered

    def send_telegram(
        self,
        message: str,
        parse_mode: str = "Markdown",
        bypass_rate_limit: bool = False,
        silent: bool = False,
        track: bool = True,
    ) -> bool:
        """Send message via Telegram bot with rate limiting

        Args:
            message: Message to send
            parse_mode: Telegram parse mode (default: Markdown)
            bypass_rate_limit: If True, skip rate limiting (for CRITICAL alerts)
            silent: If True, send without notification sound (for periodic reports)
            track: If False, do not count this send in the channel statistics
                (used for the channel-down warning itself)

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
            "disable_notification": silent,
        }

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            self._record_send("telegram", True, track=track)
            if bypass_rate_limit:
                logger.info("Telegram CRITICAL alert sent (rate limit bypassed)")
            return True
        except requests.exceptions.RequestException as e:
            crossed = self._record_send(
                "telegram", False, track=track, outage=_failure_is_outage(e)
            )
            logger.error(f"Telegram send error: {redact_error(str(e), self._secrets)}")
            if crossed:
                self._warn_channel_down("telegram")
            return False

    def send_pushover(
        self,
        message: str,
        title: str = "Monad Alert",
        priority: int = 0,
        sound: str = "pushover",
        bypass_rate_limit: bool = False,
        validator_name: Optional[str] = None,
        track: bool = True,
    ) -> bool:
        """Send message via Pushover (emergency alerts that bypass DND) with rate limiting

        Args:
            message: Message to send
            title: Alert title
            priority: Pushover priority (0=normal, 1=high, 2=emergency)
            sound: Notification sound
            bypass_rate_limit: If True, skip rate limiting (for CRITICAL alerts)
            validator_name: Optional validator name for cooldown tracking
            track: If False, do not count this send in the channel statistics

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

            self._record_send("pushover", True, track=track)
            if bypass_rate_limit:
                logger.info(f"Pushover CRITICAL alert sent for {validator_name or 'unknown'}")
            return True
        except requests.exceptions.RequestException as e:
            crossed = self._record_send(
                "pushover", False, track=track, outage=_failure_is_outage(e)
            )
            logger.error(f"Pushover send error: {redact_error(str(e), self._secrets)}")
            if crossed:
                self._warn_channel_down("pushover")
            return False

    def send_discord(
        self,
        message: str,
        title: str = "Monad Alert",
        color: int = 0x3498db,  # Blue default
        bypass_rate_limit: bool = False,
        silent: bool = False,
        track: bool = True,
    ) -> bool:
        """Send message via Discord webhook with rate limiting

        Args:
            message: Message to send
            title: Embed title
            color: Embed color (hex int, default blue)
            bypass_rate_limit: If True, skip rate limiting (for CRITICAL alerts)
            silent: If True, send without notification sound (for periodic reports)
            track: If False, do not count this send in the channel statistics

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
        # flags: 1 << 0 = SUPPRESS_NOTIFICATIONS (silent send)
        payload = {
            "embeds": [
                {
                    "title": title,
                    # Discord renders text verbatim (it has its own markdown),
                    # so the Telegram escapes must not leak through as backslashes
                    "description": unescape_markdown(message),
                    "color": color,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            ]
        }

        # Add silent flag if requested
        if silent:
            payload["flags"] = 1 << 0  # SUPPRESS_NOTIFICATIONS

        try:
            response = requests.post(self.discord_webhook_url, json=payload, timeout=10)
            response.raise_for_status()
            self._record_send("discord", True, track=track)
            if bypass_rate_limit:
                logger.info("Discord CRITICAL alert sent (rate limit bypassed)")
            return True
        except requests.exceptions.RequestException as e:
            crossed = self._record_send(
                "discord", False, track=track, outage=_failure_is_outage(e)
            )
            logger.error(f"Discord send error: {redact_error(str(e), self._secrets)}")
            if crossed:
                self._warn_channel_down("discord")
            return False

    def send_slack(
        self,
        message: str,
        title: str = "Monad Alert",
        color: str = "#3498db",  # Blue default
        bypass_rate_limit: bool = False,
        silent: bool = False,
        track: bool = True,
    ) -> bool:
        """Send message via Slack incoming webhook with rate limiting

        Args:
            message: Message to send
            title: Attachment title
            color: Attachment sidebar color (hex string, default blue)
            bypass_rate_limit: If True, skip rate limiting (for CRITICAL alerts)
            silent: Unused (Slack webhooks don't support silent mode), kept for API parity
            track: If False, do not count this send in the channel statistics

        Returns:
            True if message was sent successfully, False otherwise
        """
        if not self.slack_webhook_url:
            logger.debug("Slack webhook not configured - skipping Slack alert")
            return False

        # Check rate limit (unless bypassed for critical alerts)
        if not bypass_rate_limit:
            if not self._slack_limiter.consume(1):
                logger.warning("Slack rate limit exceeded - message dropped")
                return False

        payload = {
            "attachments": [
                {
                    "title": title,
                    # Slack has no backslash escape, so Telegram escapes must not leak
                    "text": unescape_markdown(message),
                    "color": color,
                    "ts": int(time.time()),
                }
            ]
        }

        try:
            response = requests.post(self.slack_webhook_url, json=payload, timeout=10)
            response.raise_for_status()
            self._record_send("slack", True, track=track)
            if bypass_rate_limit:
                logger.info("Slack CRITICAL alert sent (rate limit bypassed)")
            return True
        except requests.exceptions.RequestException as e:
            crossed = self._record_send(
                "slack", False, track=track, outage=_failure_is_outage(e)
            )
            logger.error(f"Slack send error: {redact_error(str(e), self._secrets)}")
            if crossed:
                self._warn_channel_down("slack")
            return False

    def alert_warning(self, message: str) -> bool:
        """Send warning alert (Telegram + Discord + Slack, rate limited)

        Returns:
            True if sent successfully to at least one channel, False otherwise
        """
        telegram_success = self.send_telegram(f"⚠️ *WARNING*\n\n{message}")
        discord_success = self.send_discord(
            message=message,
            title="⚠️ MONAD WARNING",
            color=0xf39c12,  # Orange for warning
        )
        slack_success = self.send_slack(
            message=message,
            title="⚠️ MONAD WARNING",
            color="#f39c12",
        )
        return telegram_success or discord_success or slack_success

    def alert_critical(self, message: str, validator_name: Optional[str] = None) -> bool:
        """Send critical alert (Telegram + Pushover + Discord + Slack)

        Telegram: Bypasses rate limit (never miss critical alerts)
        Pushover: Has 30-minute cooldown per validator to prevent alert storms
        Discord/Slack: Bypasses rate limit for critical alerts

        Args:
            message: Alert message to send
            validator_name: Optional validator name for Pushover cooldown tracking

        Returns:
            True if at least one channel sent successfully, False otherwise
        """
        telegram_success = False
        pushover_success = False
        discord_success = False
        slack_success = False

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

        # Slack alert (bypasses rate limit for critical)
        slack_success = self.send_slack(
            message=message,
            title="🔴 MONAD CRITICAL ALERT",
            color="#e74c3c",
            bypass_rate_limit=True,
        )

        # Track for monitoring
        if telegram_success or pushover_success or discord_success or slack_success:
            self._critical_alerts_sent += 1
            return True
        else:
            self._critical_alerts_dropped += 1
            logger.error("CRITICAL alert failed to send on ALL channels!")
            # Queue for retry to prevent alert loss
            self._queue_failed_alert(message, validator_name)
            return False

    def alert_info(self, message: str) -> bool:
        """Send info alert (Telegram + Discord + Slack, rate limited)

        Returns:
            True if sent successfully to at least one channel, False otherwise
        """
        telegram_success = self.send_telegram(f"ℹ️ *INFO*\n\n{message}")
        discord_success = self.send_discord(
            message=message,
            title="ℹ️ MONAD INFO",
            color=0x3498db,  # Blue for info
        )
        slack_success = self.send_slack(
            message=message,
            title="ℹ️ MONAD INFO",
            color="#3498db",
        )
        return telegram_success or discord_success or slack_success

    def alert_update(self, message: str) -> bool:
        """Send version update notification (Telegram + Discord + Slack, rate limited)

        Pushover is intentionally NOT used - it is reserved for critical/emergency
        alerts, so update notices do not wake operators.

        Returns:
            True if sent successfully to at least one channel, False otherwise
        """
        telegram_success = self.send_telegram(f"🆕 *MONAD MONITOR UPDATE*\n\n{message}")
        discord_success = self.send_discord(
            message=message,
            title="🆕 MONAD MONITOR UPDATE",
            color=0x2ecc71,  # Green for update
        )
        slack_success = self.send_slack(
            message=message,
            title="🆕 MONAD MONITOR UPDATE",
            color="#2ecc71",
        )
        return telegram_success or discord_success or slack_success

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

    def _load_failed_alerts(self) -> None:
        """Load the retry queue from the state volume, dropping stale entries.

        Fail-open: a missing, unreadable or corrupt file leaves an empty queue
        (the monitor must never fail to start because of it).
        """
        if not self._failed_alerts_path or not os.path.exists(self._failed_alerts_path):
            return
        try:
            with open(self._failed_alerts_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning(
                f"Could not read the failed-alert queue "
                f"({self._failed_alerts_path}): {exc} - starting with an empty queue"
            )
            return

        now = time.time()
        entries = data if isinstance(data, list) else []
        loaded = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            message = entry.get("message")
            failed_at = entry.get("failed_at")
            if not isinstance(message, str) or not isinstance(failed_at, (int, float)):
                continue
            if now - float(failed_at) >= FAILED_ALERT_MAX_AGE_SECONDS:
                continue
            if len(self._failed_alerts_queue) >= MAX_FAILED_ALERTS_QUEUE_SIZE:
                break
            self._failed_alerts_queue.append(
                (message, entry.get("validator"), float(failed_at))
            )
            loaded += 1
        if loaded:
            logger.info(
                f"Loaded {loaded} failed alert(s) from the retry queue "
                f"({self._failed_alerts_path})"
            )

    def _save_failed_alerts(self) -> None:
        """Persist the retry queue (atomic replace), tolerating a read-only dir.

        A failure here must not break alerting: the alert path is the whole
        point of this object, so we log once and keep the in-memory queue.
        """
        if not self._failed_alerts_path:
            return
        payload = [
            {"message": message, "validator": validator_name, "failed_at": failed_at}
            for message, validator_name, failed_at in self._failed_alerts_queue
        ]
        temp_path = f"{self._failed_alerts_path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(temp_path, self._failed_alerts_path)
            self._queue_write_failed_logged = False
        except OSError as exc:
            if not self._queue_write_failed_logged:
                logger.error(
                    f"Could not persist the failed-alert queue to "
                    f"{self._failed_alerts_path}: {exc} - retries stay in memory only"
                )
                self._queue_write_failed_logged = True

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
        self._save_failed_alerts()
        logger.info(f"Queued failed alert for retry: {validator_name or 'unknown'} (queue size: {len(self._failed_alerts_queue)})")

    def retry_failed_alerts(self) -> int:
        """Retry a bounded batch of failed alerts.

        Capped at MAX_RETRY_PER_CYCLE: each retry fans out to four channels with
        a 10s timeout, so draining a full queue in one monitoring cycle could
        stall the loop for minutes exactly when the channels are already broken.
        Unprocessed entries stay queued (and persisted) for the next cycle.

        Returns:
            Number of alerts successfully sent
        """
        if not self._failed_alerts_queue:
            return 0

        batch = self._failed_alerts_queue[:MAX_RETRY_PER_CYCLE]
        remaining = self._failed_alerts_queue[MAX_RETRY_PER_CYCLE:]
        self._failed_alerts_queue = []
        sent_count = 0

        for message, validator_name, failed_at in batch:
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

            slack_success = self.send_slack(
                message=f"[RETRY] {message}",
                title="🔴 MONAD CRITICAL ALERT (Retry)",
                color="#e74c3c",
                bypass_rate_limit=True,
            )

            if telegram_success or pushover_success or discord_success or slack_success:
                sent_count += 1
                logger.info(f"Successfully retried alert for {validator_name or 'unknown'}")
            else:
                # Still failing, re-queue if not too old
                if age_seconds < FAILED_ALERT_MAX_AGE_SECONDS:
                    remaining.append((message, validator_name, failed_at))
                    logger.warning(f"Retry failed for {validator_name or 'unknown'}, re-queued")
                else:
                    logger.error(f"Dropping stale alert for {validator_name or 'unknown'} (age: {age_seconds}s)")

        self._failed_alerts_queue.extend(remaining)
        self._save_failed_alerts()
        return sent_count

    def get_failed_queue_size(self) -> int:
        """Get the number of failed alerts waiting for retry."""
        return len(self._failed_alerts_queue)
