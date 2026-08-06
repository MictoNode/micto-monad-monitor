"""New release version checker for the monitor itself"""

import json
import os
import time
from typing import List, Optional, Tuple

import requests

from .logger import get_logger

logger = get_logger()

GHCR_TOKEN_URL = "https://ghcr.io/token"
GHCR_TAGS_URL = "https://ghcr.io/v2/{repo}/tags/list"
DEFAULT_IMAGE = "ghcr.io/mictonode/micto-monad-monitor"
DEFAULT_CHECK_INTERVAL = 7 * 24 * 3600  # Weekly
DEFAULT_CURRENT_VERSION = "0.0.0"


def normalize_version(version: str) -> Optional[Tuple[int, int, int]]:
    """Parse 'v1.4.9' or '1.4.9' into a comparable tuple.

    Returns None for anything that is not a full MAJOR.MINOR.PATCH semver.
    """
    tag = version.strip()
    if tag[:1] in ("v", "V"):
        tag = tag[1:]
    parts = tag.split(".")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def latest_release(tags: List[str]) -> Optional[str]:
    """Return the newest full-release tag from a list of registry tags.

    Ignores non-semver tags (e.g. 'latest', '1', '1.4').
    """
    candidates = []
    for tag in tags:
        ver = normalize_version(tag)
        if ver is not None:
            candidates.append((ver, tag))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def build_update_message(latest: str, current: str) -> str:
    """Build the 'new version available' notification message."""
    def _display(v: str) -> str:
        return v[1:] if v[:1] in ("v", "V") else v

    return (
        f"🆕 New monitor version v{_display(latest)} available (running: v{_display(current)})\n\n"
        f"Update:\n"
        f"cd $HOME/micto-monad-monitor/ && git pull && docker compose pull && docker compose up -d"
    )


def _repo_path(image: str) -> str:
    """Extract the repository path from a full image reference.

    'ghcr.io/mictonode/micto-monad-monitor' -> 'mictonode/micto-monad-monitor'
    """
    if image.startswith("ghcr.io/"):
        return image[len("ghcr.io/"):]
    return image


class VersionChecker:
    """Periodically check the published monitor version and notify on update.

    Fetches the published image tags from GHCR, compares the newest full
    release to the running version, and notifies exactly once per version
    (persisted in the state file).
    """

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        check_interval: int = DEFAULT_CHECK_INTERVAL,
        state_file: str = "/app/state/last_notified_version.json",
        alerts=None,
        current_version: Optional[str] = None,
    ):
        self.image = image
        self.check_interval = check_interval
        self.state_file = state_file
        self.alerts = alerts
        self.current_version = current_version or os.getenv("MONITOR_VERSION", DEFAULT_CURRENT_VERSION)
        self._last_check = 0.0
        self._last_notified: Optional[str] = None

    def maybe_notify(self) -> None:
        """Check for a new version if the check interval has elapsed."""
        now = time.time()
        if now - self._last_check < self.check_interval:
            return
        self._last_check = now

        try:
            latest = self._fetch_latest()
        except Exception as e:
            logger.warning(f"Version check failed: {e}")
            return

        if latest is None:
            logger.debug("Version check: no release tags found")
            return

        last_notified = self._load_last_notified()
        latest_tuple = normalize_version(latest)
        last_tuple = normalize_version(last_notified)

        if latest_tuple is None:
            return

        if last_tuple is None or latest_tuple > last_tuple:
            logger.info(
                f"New monitor version available: {latest} (running {self.current_version})"
            )
            if self.alerts is not None:
                self.alerts.alert_update(build_update_message(latest, self.current_version))
            self._save_last_notified(latest)

    def _fetch_latest(self) -> Optional[str]:
        """Fetch the newest full-release tag from the GHCR registry."""
        repo = _repo_path(self.image)

        token_resp = requests.get(
            GHCR_TOKEN_URL,
            params={"service": "ghcr.io", "scope": f"repository:{repo}:pull"},
            timeout=10,
        )
        token_resp.raise_for_status()
        token = token_resp.json().get("token", "")

        tags_resp = requests.get(
            GHCR_TAGS_URL.format(repo=repo),
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        tags_resp.raise_for_status()
        tags = tags_resp.json().get("tags", [])
        return latest_release(tags)

    def _load_last_notified(self) -> str:
        """Return the last notified version, defaulting to the current one."""
        if self._last_notified is not None:
            return self._last_notified
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
                self._last_notified = str(data.get("version", ""))
        except (FileNotFoundError, ValueError, OSError):
            pass
        if self._last_notified is None:
            self._last_notified = self.current_version or DEFAULT_CURRENT_VERSION
        return self._last_notified

    def _save_last_notified(self, version: str) -> None:
        """Persist the last notified version to the state file."""
        self._last_notified = version
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
            data = {"version": version, "notified_at": time.time()}
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError as e:
            logger.warning(f"Failed to persist last notified version: {e}")
