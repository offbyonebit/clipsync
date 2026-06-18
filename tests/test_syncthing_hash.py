"""Tests for the Syncthing archive hash verification.

Uses mocked network calls so the tests are hermetic; the real fetch
logic is exercised end-to-end in the integration tests.
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from clipsync import syncthing
from clipsync.syncthing import SyncthingError

_SAMPLE_SHA256_CONTENT = (
    "-----BEGIN PGP SIGNED MESSAGE-----\n"
    "Hash: SHA256\n"
    "\n"
    "d5ca379993844b0e6e4fced05e3ac4a6c4513dee916ab65516c6d07d5e53e317  syncthing-linux-amd64-v2.0.16.tar.gz\n"
    "2b5fe419de35c26354843ae567b2ae5c1bf82b151e3aea3dcfb620ca590999d4  syncthing-macos-amd64-v2.0.16.zip\n"
    "5b519408c11e69e712702911caa399077e3fc602d8d70d6147f620e67bd83037  syncthing-windows-amd64-v2.0.16.zip\n"
    "-----BEGIN PGP SIGNATURE-----\n"
    "irrelevant\n"
    "-----END PGP SIGNATURE-----\n"
)


def test_fetch_official_sha256sums_parses_pgp_wrapped_file() -> None:
    with patch("clipsync.syncthing._download", return_value=_SAMPLE_SHA256_CONTENT.encode("ascii")):
        sums = syncthing._fetch_official_sha256sums("v2.0.16")
    assert sums == {
        "syncthing-linux-amd64-v2.0.16.tar.gz": "d5ca379993844b0e6e4fced05e3ac4a6c4513dee916ab65516c6d07d5e53e317",
        "syncthing-macos-amd64-v2.0.16.zip": "2b5fe419de35c26354843ae567b2ae5c1bf82b151e3aea3dcfb620ca590999d4",
        "syncthing-windows-amd64-v2.0.16.zip": "5b519408c11e69e712702911caa399077e3fc602d8d70d6147f620e67bd83037",
    }


def test_fetch_official_sha256sums_normalizes_v_prefix() -> None:
    """Version may be passed with or without the leading 'v'."""
    seen_urls = []

    def fake_download(url: str) -> bytes:
        seen_urls.append(url)
        return _SAMPLE_SHA256_CONTENT.encode("ascii")

    with patch("clipsync.syncthing._download", side_effect=fake_download):
        syncthing._fetch_official_sha256sums("2.0.16")
    assert seen_urls == ["https://github.com/syncthing/syncthing/releases/download/v2.0.16/sha256sum.txt.asc"]


def test_fetch_official_sha256sums_returns_empty_on_network_error() -> None:
    from urllib.error import URLError

    with patch("clipsync.syncthing._download", side_effect=URLError("offline")):
        sums = syncthing._fetch_official_sha256sums("v2.0.16")
    assert sums == {}


def test_fetch_official_sha256sums_ignores_malformed_lines() -> None:
    bad_content = (
        "-----BEGIN PGP SIGNED MESSAGE-----\n"
        "Hash: SHA256\n"
        "\n"
        "not a hash line\n"
        "abc123  short-hash\n"
        "d5ca379993844b0e6e4fced05e3ac4a6c4513dee916ab65516c6d07d5e53e317  syncthing-linux-amd64-v2.0.16.tar.gz\n"
    )
    with patch("clipsync.syncthing._download", return_value=bad_content.encode("ascii")):
        sums = syncthing._fetch_official_sha256sums("v2.0.16")
    assert sums == {
        "syncthing-linux-amd64-v2.0.16.tar.gz": "d5ca379993844b0e6e4fced05e3ac4a6c4513dee916ab65516c6d07d5e53e317",
    }


def test_verify_archive_hash_succeeds_on_match(monkeypatch) -> None:
    archive = b"the bytes of a syncthing archive"
    expected = hashlib.sha256(archive).hexdigest()
    name = "syncthing-linux-amd64-v2.0.16.tar.gz"

    monkeypatch.setattr(syncthing, "_fetch_official_sha256sums", lambda _v: {name: expected})
    # Should not raise.
    syncthing._verify_archive_hash(archive, "v2.0.16")


def test_verify_archive_hash_raises_on_mismatch(monkeypatch) -> None:
    archive = b"the bytes of a syncthing archive"
    name = "syncthing-linux-amd64-v2.0.16.tar.gz"

    monkeypatch.setattr(syncthing, "_fetch_official_sha256sums", lambda _v: {name: "0" * 64})
    with pytest.raises(SyncthingError, match="hash mismatch"):
        syncthing._verify_archive_hash(archive, "v2.0.16")


def test_verify_archive_hash_skips_when_platform_absent(monkeypatch) -> None:
    """If sha256sum.txt has no entry for our platform, verification must
    be skipped (logged) rather than fail. Otherwise an unusual platform
    would be unable to install even when Syncthing ships a binary for it."""
    archive = b"some bytes"
    monkeypatch.setattr(syncthing, "_fetch_official_sha256sums", lambda _v: {})
    # Should not raise.
    syncthing._verify_archive_hash(archive, "v2.0.16")


def test_verify_archive_hash_skips_when_fetch_fails(monkeypatch) -> None:
    archive = b"some bytes"
    monkeypatch.setattr(syncthing, "_fetch_official_sha256sums", lambda _v: {})
    syncthing._verify_archive_hash(archive, "v2.0.16")


def test_archive_filename_matches_release_naming() -> None:
    name = syncthing._archive_filename("v2.0.16")
    # Must match the asset names Syncthing actually publishes.
    assert name in {
        "syncthing-linux-amd64-v2.0.16.tar.gz",
        "syncthing-macos-amd64-v2.0.16.zip",
        "syncthing-windows-amd64-v2.0.16.zip",
        "syncthing-linux-arm64-v2.0.16.tar.gz",
        "syncthing-macos-arm64-v2.0.16.zip",
        "syncthing-windows-arm64-v2.0.16.zip",
    }
