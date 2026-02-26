"""Token Bucket Rate Limiter for API call throttling"""

import time
import threading
from dataclasses import dataclass
from typing import Optional


@dataclass
class TokenBucketRateLimiter:
    """
    Thread-safe token bucket rate limiter.

    Allows burst traffic up to max_tokens, then refills at refill_rate tokens/second.

    Usage:
        limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=1.0)
        if limiter.can_consume():
            send_alert()
            limiter.consume(1)
    """

    max_tokens: float
    refill_rate: float  # Tokens per second
    tokens: float = 0.0
    last_refill: float = 0.0
    _lock: threading.Lock = None  # type: ignore

    def __post_init__(self):
        self.tokens = self.max_tokens
        self.last_refill = time.time()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time (internal method)"""
        now = time.time()
        elapsed = now - self.last_refill
        new_tokens = elapsed * self.refill_rate
        self.tokens = min(self.max_tokens, self.tokens + new_tokens)
        self.last_refill = now

    def can_consume(self, tokens: int = 1) -> bool:
        """Check if tokens are available without consuming"""
        with self._lock:
            self._refill()
            return self.tokens >= tokens

    def consume(self, tokens: int = 1) -> bool:
        """
        Try to consume tokens. Returns True if successful, False if insufficient tokens.
        Does not consume on failure.
        """
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def consume_or_wait(self, tokens: int = 1, max_wait: float = 0.0) -> bool:
        """
        Try to consume tokens, waiting up to max_wait seconds if necessary.

        NOTE: This method has inherent race condition semantics. After waiting,
        another thread may have consumed the tokens. In that case, the method
        returns False without consuming. This is intentional - it provides
        "best effort" waiting without blocking other consumers indefinitely.

        For guaranteed consumption, use consume() in a loop with your own
        waiting logic, or consider using a semaphore instead.

        Args:
            tokens: Number of tokens to consume
            max_wait: Maximum time to wait in seconds (0 = no wait)

        Returns:
            True if tokens were consumed, False if timeout or tokens unavailable
        """
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True

            if max_wait <= 0:
                return False

            # Calculate wait time needed
            tokens_needed = tokens - self.tokens
            wait_time = min(max_wait, tokens_needed / self.refill_rate)

            if wait_time > max_wait:
                return False

        # Wait outside the lock to allow other threads to consume
        time.sleep(wait_time)

        # Try again after waiting (may fail if another thread consumed tokens)
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def remaining_tokens(self) -> float:
        """Get current number of available tokens (after refill)"""
        with self._lock:
            self._refill()
            return self.tokens

    def time_until_available(self, tokens: int = 1) -> float:
        """
        Calculate how long until tokens are available.
        Returns 0 if tokens are already available.
        """
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                return 0.0
            tokens_needed = tokens - self.tokens
            return tokens_needed / self.refill_rate

    def reset(self) -> None:
        """Reset bucket to full capacity"""
        with self._lock:
            self.tokens = self.max_tokens
            self.last_refill = time.time()

    @classmethod
    def telegram_rate_limiter(cls) -> "TokenBucketRateLimiter":
        """
        Create a rate limiter for Telegram API.
        Profile: 10 alerts per minute (burst), 10/min sustained
        """
        return cls(max_tokens=10, refill_rate=10.0 / 60.0)  # ~0.167 tokens/sec

    @classmethod
    def pushover_rate_limiter(cls) -> "TokenBucketRateLimiter":
        """
        Create a rate limiter for Pushover API.
        Profile: 5 alerts per minute (burst), 5/min sustained
        Pushover has stricter limits for emergency priority.
        """
        return cls(max_tokens=5, refill_rate=5.0 / 60.0)  # ~0.083 tokens/sec
