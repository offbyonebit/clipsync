"""In-app update checker.

Queries the GitHub Releases API for the project's latest published release
and compares its tag to the running version. The UI surfaces the result and,
when an update exists, offers to open the release page in the default
browser. Self-install isn't attempted: the PyInstaller bundle varies by
platform and replacing a running binary mid-flight is fragile.
"""

from __future__ import annotations

import logging
import re
import webbrowser
from dataclasses import dataclass

import requests

from . import __version__

log = logging.getLogger(__name__)

GITHUB_OWNER = "offbyonebit"
GITHUB_REPO = "clipsync"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_HTML_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

REQUEST_TIMEOUT = 10


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    update_available: bool
    release_url: str


def _parse_version(tag: str) -> tuple[int, ...]:
    """Parse a release tag into a comparable tuple.

    Accepts "v1.2.3", "1.2.3", and "1.2.3-rc1" (pre-release suffix ignored).
    Unparseable input returns (0,), which sorts below any real release.
    """
    cleaned = tag.strip().lstrip("vV")
    cleaned = re.split(r"[-+]", cleaned, maxsplit=1)[0]
    parts = cleaned.split(".")
    out: list[int] = []
    for p in parts:
        if not p.isdigit():
            return (0,)
        out.append(int(p))
    return tuple(out) if out else (0,)


def _is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def check_for_update(
    current_version: str = __version__,
    url: str = RELEASES_API_URL,
) -> UpdateInfo:
    """Hit the GitHub Releases API and return the comparison result.

    Raises requests.RequestException on network failure so the caller
    can surface a clear "couldn't reach GitHub" message."""
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"Accept": "application/vnd.github+json"})
    resp.raise_for_status()
    data = resp.json()
    tag = str(data.get("tag_name") or "").strip()
    release_url = str(data.get("html_url") or RELEASES_HTML_URL)
    if not tag:
        log.warning("Latest release has no tag_name, treating as up to date")
        return UpdateInfo(current_version, current_version, False, release_url)
    latest = tag.lstrip("vV")
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest,
        update_available=_is_newer(latest, current_version),
        release_url=release_url,
    )


def open_download_page(url: str = RELEASES_HTML_URL) -> bool:
    """Open the release page in the user's default browser."""
    try:
        return webbrowser.open(url, new=2)
    except Exception:
        log.exception("Failed to open browser for %s", url)
        return False
