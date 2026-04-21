"""Tests for the GitHub Releases update checker."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from clipsync import update


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("0.2.0", "0.1.0", True),
        ("0.1.1", "0.1.0", True),
        ("1.0.0", "0.9.9", True),
        ("0.1.0", "0.1.0", False),
        ("0.1.0", "0.2.0", False),
        ("v0.2.0", "0.1.0", True),
        ("0.2.0-rc1", "0.1.0", True),
        ("bogus", "0.1.0", False),
        ("", "0.1.0", False),
    ],
)
def test_is_newer(latest: str, current: str, expected: bool) -> None:
    assert update._is_newer(latest, current) is expected


def test_check_for_update_reports_newer_release() -> None:
    response = MagicMock()
    response.json.return_value = {
        "tag_name": "v0.2.0",
        "html_url": "https://github.com/offbyonebit/clipsync/releases/tag/v0.2.0",
    }
    response.raise_for_status.return_value = None

    with patch("clipsync.update.requests.get", return_value=response) as mock_get:
        info = update.check_for_update(current_version="0.1.0")

    mock_get.assert_called_once()
    assert info.update_available is True
    assert info.latest_version == "0.2.0"
    assert info.current_version == "0.1.0"
    assert info.release_url.endswith("/v0.2.0")


def test_check_for_update_reports_up_to_date() -> None:
    response = MagicMock()
    response.json.return_value = {"tag_name": "v0.1.0", "html_url": "x"}
    response.raise_for_status.return_value = None
    with patch("clipsync.update.requests.get", return_value=response):
        info = update.check_for_update(current_version="0.1.0")
    assert info.update_available is False
    assert info.latest_version == "0.1.0"


def test_check_for_update_missing_tag_is_treated_as_up_to_date() -> None:
    response = MagicMock()
    response.json.return_value = {"html_url": "x"}
    response.raise_for_status.return_value = None
    with patch("clipsync.update.requests.get", return_value=response):
        info = update.check_for_update(current_version="0.1.0")
    assert info.update_available is False


def test_check_for_update_raises_on_network_error() -> None:
    with (
        patch("clipsync.update.requests.get", side_effect=requests.ConnectionError("boom")),
        pytest.raises(requests.ConnectionError),
    ):
        update.check_for_update(current_version="0.1.0")


def test_open_download_page_uses_webbrowser() -> None:
    with patch("clipsync.update.webbrowser.open", return_value=True) as mock_open:
        assert update.open_download_page("https://example.com") is True
    mock_open.assert_called_once_with("https://example.com", new=2)


def test_open_download_page_swallows_exceptions() -> None:
    with patch("clipsync.update.webbrowser.open", side_effect=RuntimeError("x")):
        assert update.open_download_page("https://example.com") is False
