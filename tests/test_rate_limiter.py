"""Tests for Token Bucket Rate Limiter"""

import time
import pytest

from monad_monitor.rate_limiter import TokenBucketRateLimiter


class TestTokenBucketRateLimiter:
    """Test cases for TokenBucketRateLimiter"""

    def test_initial_state_full_bucket(self):
        """Test that bucket starts full"""
        limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=1.0)
        assert limiter.tokens == 10
        assert limiter.can_consume() is True

    def test_consume_single_token(self):
        """Test consuming a single token"""
        limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=1.0)
        result = limiter.consume(1)
        assert result is True
        assert limiter.tokens == 9

    def test_consume_multiple_tokens(self):
        """Test consuming multiple tokens at once"""
        limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=1.0)
        result = limiter.consume(5)
        assert result is True
        assert limiter.tokens == 5

    def test_consume_more_than_available_fails(self):
        """Test that consuming more than available fails gracefully"""
        limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=1.0)
        result = limiter.consume(15)
        assert result is False
        assert limiter.tokens == 10  # No tokens consumed on failure

    def test_can_consume_returns_false_when_empty(self):
        """Test can_consume returns False when bucket is empty"""
        limiter = TokenBucketRateLimiter(max_tokens=5, refill_rate=1.0)
        limiter.consume(5)
        assert limiter.tokens == 0
        assert limiter.can_consume() is False

    def test_refill_over_time(self):
        """Test that tokens refill over time"""
        limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=10.0)  # 10 tokens/sec
        limiter.consume(10)  # Empty the bucket
        assert limiter.tokens == 0

        # Wait 0.5 seconds - should have ~5 tokens
        time.sleep(0.5)
        limiter._refill()
        assert 4 <= limiter.tokens <= 6  # Allow for timing variance

    def test_refill_does_not_exceed_max(self):
        """Test that refill doesn't exceed max tokens"""
        limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=100.0)
        limiter.consume(5)
        time.sleep(0.5)
        limiter._refill()
        assert limiter.tokens <= 10

    def test_remaining_tokens(self):
        """Test remaining_tokens method"""
        limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=1.0)
        limiter.consume(3)
        # Allow for small timing variance due to refill calculation
        assert 6.99 <= limiter.remaining_tokens() <= 7.01

    def test_time_until_available_when_empty(self):
        """Test time_until_available returns correct estimate"""
        limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=2.0)  # 2 tokens/sec
        limiter.consume(10)  # Empty
        wait_time = limiter.time_until_available(4)
        # Need 4 tokens at 2/sec = 2 seconds
        assert 1.9 <= wait_time <= 2.1

    def test_time_until_available_when_sufficient(self):
        """Test time_until_available returns 0 when tokens available"""
        limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=1.0)
        assert limiter.time_until_available(5) == 0

    def test_thread_safety(self):
        """Test that rate limiter is thread-safe"""
        import threading

        limiter = TokenBucketRateLimiter(max_tokens=100, refill_rate=0.0)  # No refill for predictable test
        successes = []
        failures = []
        lock = threading.Lock()

        def consume_tokens():
            for _ in range(20):
                if limiter.consume(1):
                    with lock:
                        successes.append(1)
                else:
                    with lock:
                        failures.append(1)

        threads = [threading.Thread(target=consume_tokens) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have exactly 100 successes (initial bucket size)
        # No more than max_tokens should be consumed
        assert len(successes) == 100
        assert len(failures) == 100  # 200 total attempts - 100 successes

    def test_telegram_rate_limit_profile(self):
        """Test Telegram rate limit profile: 10 alerts per minute"""
        limiter = TokenBucketRateLimiter.telegram_rate_limiter()
        assert limiter.max_tokens == 10
        assert limiter.refill_rate == pytest.approx(10.0 / 60.0, rel=0.1)

    def test_pushover_rate_limit_profile(self):
        """Test Pushover rate limit profile: 5 alerts per minute"""
        limiter = TokenBucketRateLimiter.pushover_rate_limiter()
        assert limiter.max_tokens == 5
        assert limiter.refill_rate == pytest.approx(5.0 / 60.0, rel=0.1)

    def test_consume_or_wait_returns_true_immediately(self):
        """Test consume_or_wait returns True immediately when tokens available"""
        limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=1.0)
        result = limiter.consume_or_wait(1, max_wait=1.0)
        assert result is True
        assert limiter.tokens == 9

    def test_consume_or_wait_returns_false_on_timeout(self):
        """Test consume_or_wait returns False when max_wait exceeded"""
        limiter = TokenBucketRateLimiter(max_tokens=1, refill_rate=0.01)  # Very slow refill
        limiter.consume(1)  # Empty the bucket
        result = limiter.consume_or_wait(1, max_wait=0.1)  # Would need ~100s to refill
        assert result is False

    def test_reset_bucket(self):
        """Test reset functionality"""
        limiter = TokenBucketRateLimiter(max_tokens=10, refill_rate=1.0)
        limiter.consume(10)
        assert limiter.tokens == 0
        limiter.reset()
        assert limiter.tokens == 10
