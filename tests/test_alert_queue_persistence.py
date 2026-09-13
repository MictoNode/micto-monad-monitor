"""Failed-alert queue persistence (B3)

A CRITICAL that no channel accepted is queued for retry. The queue lives on the
state volume so a restart, a crash or a long outage cannot lose it; retries are
bounded per cycle so draining the queue cannot stall the monitor loop.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import responses

from monad_monitor.alerts import (
    FAILED_ALERT_MAX_AGE_SECONDS,
    MAX_FAILED_ALERTS_QUEUE_SIZE,
    MAX_RETRY_PER_CYCLE,
    AlertHandler,
)

TELEGRAM_URL = "https://api.telegram.org/bottest-token/sendMessage"


def make_handler(path: Optional[Path] = None) -> AlertHandler:
    """Telegram-only handler (one configured channel is enough to fail)"""
    return AlertHandler(
        telegram_token="test-token",
        telegram_chat_id="chat",
        failed_alerts_path=str(path) if path else None,
    )


def failing_critical(handler: AlertHandler) -> bool:
    """A CRITICAL where every configured channel fails -> queued for retry"""
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, TELEGRAM_URL, json={}, status=500)
        return handler.alert_critical("boom")


class TestQueuePersistence:
    def test_queue_survives_a_restart(self, tmp_path):
        path = tmp_path / "failed_alerts.json"

        first = make_handler(path)
        assert failing_critical(first) is False
        assert first.get_failed_queue_size() == 1
        assert path.exists()

        second = make_handler(path)
        assert second.get_failed_queue_size() == 1
        message, validator_name, failed_at = second._failed_alerts_queue[0]
        assert message == "boom"
        assert failed_at > 0

    def test_stale_entries_are_dropped_on_load(self, tmp_path):
        path = tmp_path / "failed_alerts.json"
        now = time.time()
        path.write_text(
            json.dumps(
                [
                    {
                        "message": "old",
                        "validator": "v",
                        "failed_at": now - FAILED_ALERT_MAX_AGE_SECONDS - 10,
                    },
                    {"message": "fresh", "validator": "v", "failed_at": now - 5},
                ]
            )
        )

        handler = make_handler(path)

        assert handler.get_failed_queue_size() == 1
        assert handler._failed_alerts_queue[0][0] == "fresh"

    def test_corrupt_file_starts_with_an_empty_queue(self, tmp_path, caplog):
        path = tmp_path / "failed_alerts.json"
        path.write_text("{ this is not json")

        with caplog.at_level(logging.WARNING):
            handler = make_handler(path)

        assert handler.get_failed_queue_size() == 0
        assert any("Could not read the failed-alert queue" in r.getMessage() for r in caplog.records)

    def test_unwritable_path_still_queues_in_memory_and_logs(self, tmp_path, caplog):
        # A directory path makes open() raise IsADirectoryError (an OSError) for
        # any user, so this holds whether the suite runs as root or not
        handler = make_handler(tmp_path)

        with caplog.at_level(logging.ERROR):
            assert failing_critical(handler) is False

        assert handler.get_failed_queue_size() == 1
        assert any("Could not persist the failed-alert queue" in r.getMessage() for r in caplog.records)

    def test_queue_is_capped_and_the_file_matches_memory(self, tmp_path):
        path = tmp_path / "queue.json"
        handler = make_handler(path)

        for index in range(MAX_FAILED_ALERTS_QUEUE_SIZE + 3):
            failing_critical(handler)

        assert handler.get_failed_queue_size() == MAX_FAILED_ALERTS_QUEUE_SIZE
        persisted = json.loads(path.read_text())
        assert len(persisted) == MAX_FAILED_ALERTS_QUEUE_SIZE
        assert persisted[-1]["message"] == f"boom"

    def test_successful_retry_clears_the_persisted_queue(self, tmp_path):
        path = tmp_path / "queue.json"
        handler = make_handler(path)
        failing_critical(handler)
        assert json.loads(path.read_text())

        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, TELEGRAM_URL, json={"ok": True}, status=200)
            assert handler.retry_failed_alerts() == 1

        assert json.loads(path.read_text()) == []
        assert handler.get_failed_queue_size() == 0

    def test_retry_batch_is_capped_per_cycle(self, tmp_path):
        handler = make_handler(tmp_path / "queue.json")
        total = MAX_RETRY_PER_CYCLE + 2
        for _ in range(total):
            failing_critical(handler)
        assert handler.get_failed_queue_size() == total

        with responses.RequestsMock() as rsps:
            for _ in range(MAX_RETRY_PER_CYCLE):
                rsps.add(responses.POST, TELEGRAM_URL, json={"ok": True}, status=200)
            sent = handler.retry_failed_alerts()
            attempts = len(rsps.calls)

        assert sent == MAX_RETRY_PER_CYCLE
        assert attempts == MAX_RETRY_PER_CYCLE
        assert handler.get_failed_queue_size() == total - MAX_RETRY_PER_CYCLE

    def test_queue_without_a_path_is_memory_only(self):
        handler = make_handler(None)
        failing_critical(handler)
        assert handler.get_failed_queue_size() == 1
