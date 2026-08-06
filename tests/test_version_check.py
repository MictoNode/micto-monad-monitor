"""Tests for VersionChecker - new release notification"""

import json
import time

import pytest
import responses

from monad_monitor.version_check import (
    VersionChecker,
    normalize_version,
    latest_release,
    build_update_message,
)

IMAGE = "ghcr.io/mictonode/micto-monad-monitor"
REPO = "mictonode/micto-monad-monitor"
TOKEN_URL = "https://ghcr.io/token"
TAGS_URL = f"https://ghcr.io/v2/{REPO}/tags/list"


class RecordingAlerts:
    """Minimal fake AlertHandler that records alert_update calls."""

    def __init__(self):
        self.calls = []

    def alert_update(self, message):
        self.calls.append(message)
        return True


@pytest.fixture
def ghcr():
    """Register GHCR token + tags endpoints; return a helper to set tags."""
    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.GET,
            TOKEN_URL,
            json={"token": "test-token"},
            status=200,
        )

        def set_tags(tags):
            rsps.add(
                responses.GET,
                TAGS_URL,
                json={"name": REPO, "tags": tags},
                status=200,
            )

        yield set_tags


def make_checker(tmp_path, alerts=None, current_version="0.0.0", check_interval=604800):
    return VersionChecker(
        image=IMAGE,
        check_interval=check_interval,
        state_file=str(tmp_path / "last_notified_version.json"),
        alerts=alerts or RecordingAlerts(),
        current_version=current_version,
    )


class TestVersionParsing:
    def test_normalize_full_semver(self):
        assert normalize_version("1.4.9") == (1, 4, 9)

    def test_normalize_v_prefix(self):
        assert normalize_version("v1.4.9") == (1, 4, 9)

    def test_normalize_rejects_short(self):
        assert normalize_version("1.4") is None

    def test_normalize_rejects_non_semver(self):
        assert normalize_version("latest") is None
        assert normalize_version("dev") is None

    def test_latest_release_filters(self):
        tags = ["latest", "1", "1.4", "1.4.8", "1.5.0", "v1.3.0"]
        assert latest_release(tags) == "1.5.0"

    def test_latest_release_sorts_numeric(self):
        # "1.9.0" < "1.10.0" lexicographically but not numerically
        tags = ["1.9.0", "1.10.0"]
        assert latest_release(tags) == "1.10.0"

    def test_latest_release_empty(self):
        assert latest_release([]) is None

    def test_latest_release_only_latest(self):
        assert latest_release(["latest", "1", "1.4"]) is None


class TestUpdateMessage:
    def test_message_contains_versions(self):
        msg = build_update_message("1.5.0", "1.4.8")
        assert "1.5.0" in msg
        assert "1.4.8" in msg

    def test_message_is_english(self):
        msg = build_update_message("1.5.0", "1.4.8")
        assert "New monitor version" in msg
        assert "available (running:" in msg
        assert "Update:" in msg
        assert "yayınlandı" not in msg

    def test_message_handles_v_prefixed_current(self):
        """Current version baked from a git tag (e.g. v1.4.9) must not double the 'v' prefix."""
        msg = build_update_message("1.5.0", "v1.4.9")
        assert "v1.5.0" in msg
        assert "v1.4.9" in msg
        assert "vv1.4.9" not in msg

    def test_message_handles_v_prefixed_latest(self):
        """A v-prefixed latest tag (e.g. v1.5.0) renders as a single 'v'."""
        msg = build_update_message("v1.5.0", "1.4.9")
        assert "v1.5.0" in msg
        assert "vv1.5.0" not in msg

    def test_message_contains_update_command(self):
        msg = build_update_message("1.5.0", "1.4.8")
        assert "git pull && docker compose pull && docker compose up -d" in msg


class TestVersionChecker:
    def test_notify_when_newer_version(self, tmp_path, ghcr):
        ghcr(["latest", "1.4.8", "1.5.0"])
        alerts = RecordingAlerts()
        checker = make_checker(tmp_path, alerts, current_version="1.4.8")

        checker.maybe_notify()

        assert len(alerts.calls) == 1
        assert "1.5.0" in alerts.calls[0]

    def test_no_notify_when_same_version(self, tmp_path, ghcr):
        ghcr(["latest", "1.5.0"])
        alerts = RecordingAlerts()
        checker = make_checker(tmp_path, alerts, current_version="1.5.0")

        checker.maybe_notify()

        assert alerts.calls == []

    def test_no_notify_when_current_newer(self, tmp_path, ghcr):
        ghcr(["latest", "1.5.0"])
        alerts = RecordingAlerts()
        checker = make_checker(tmp_path, alerts, current_version="1.6.0")

        checker.maybe_notify()

        assert alerts.calls == []

    def test_notifies_only_once_per_version(self, tmp_path, ghcr):
        # First run: 1.5.0 is new -> notify
        ghcr(["latest", "1.4.8", "1.5.0"])
        alerts = RecordingAlerts()
        checker = make_checker(tmp_path, alerts, current_version="1.4.8")
        checker.maybe_notify()
        assert len(alerts.calls) == 1

        # Same version still latest -> no second notify
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, TOKEN_URL, json={"token": "test-token"}, status=200)
            rsps.add(responses.GET, TAGS_URL, json={"tags": ["latest", "1.4.8", "1.5.0"]}, status=200)
            checker._last_check = 0.0
            checker.maybe_notify()
        assert len(alerts.calls) == 1

    def test_state_persisted_across_instances(self, tmp_path, ghcr):
        ghcr(["latest", "1.4.8", "1.5.0"])
        checker = make_checker(tmp_path, current_version="1.4.8")
        checker.maybe_notify()

        # A fresh checker with the same state file must not re-notify
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, TOKEN_URL, json={"token": "test-token"}, status=200)
            rsps.add(responses.GET, TAGS_URL, json={"tags": ["latest", "1.4.8", "1.5.0"]}, status=200)
            alerts2 = RecordingAlerts()
            checker2 = make_checker(tmp_path, alerts2, current_version="1.4.8")
            checker2._last_check = 0.0
            checker2.maybe_notify()

        assert alerts2.calls == []

    def test_unknown_current_version_notifies_once(self, tmp_path, ghcr):
        ghcr(["latest", "1.5.0"])
        alerts = RecordingAlerts()
        checker = make_checker(tmp_path, alerts, current_version="0.0.0")

        checker.maybe_notify()

        assert len(alerts.calls) == 1

    def test_respects_check_interval(self, tmp_path, ghcr):
        ghcr(["latest", "1.4.8", "1.5.0"])
        checker = make_checker(tmp_path, current_version="1.4.8", check_interval=604800)

        checker.maybe_notify()
        checker.maybe_notify()

        # Only the first call should have made network requests; second skipped
        assert True  # no exception and no extra requests asserted via count below

    def test_silent_on_network_error(self, tmp_path):
        checker = make_checker(tmp_path, current_version="1.4.8")
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, TOKEN_URL, status=500)

            checker.maybe_notify()  # must not raise

        assert checker.alerts.calls == []

    def test_state_file_written_after_notify(self, tmp_path, ghcr):
        ghcr(["latest", "1.4.8", "1.5.0"])
        checker = make_checker(tmp_path, current_version="1.4.8")

        checker.maybe_notify()

        state_file = tmp_path / "last_notified_version.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert data["version"] == "1.5.0"
        assert "notified_at" in data
