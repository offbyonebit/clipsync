"""The pinned Syncthing hash manifest is the trust anchor for the binary.

Runtime signature verification needs gpg on PATH, which stock Windows and
macOS do not have, so for most users it degraded to hash-only with the hash
coming from the same origin as the download. These tests pin down the
manifest's role: it must be used, it must be complete for the version we
ship, and it must never silently disappear.
"""

from __future__ import annotations

import hashlib

import pytest

from clipsync import config, syncthing
from clipsync._syncthing_hashes import SYNCTHING_PINNED_SHA256
from clipsync.syncthing import SyncthingError


def test_pinned_manifest_covers_the_shipped_version():
    """A version bump that forgets to regenerate the manifest would silently
    fall back to the network path this change exists to remove."""
    version = config.SYNCTHING_VERSION
    v = version if version.startswith("v") else f"v{version}"
    assert v in SYNCTHING_PINNED_SHA256, f"No pinned hashes for {v}. Run: python tools/refresh_syncthing_hashes.py {v}"


def test_pinned_manifest_covers_this_platform():
    name = syncthing._archive_filename(config.SYNCTHING_VERSION)
    v = config.SYNCTHING_VERSION
    v = v if v.startswith("v") else f"v{v}"
    assert name in SYNCTHING_PINNED_SHA256[v], f"{name} missing from the pinned manifest"


def test_pinned_hashes_are_wellformed_sha256():
    for version, archives in SYNCTHING_PINNED_SHA256.items():
        assert archives, f"{version} has an empty archive map"
        for name, digest in archives.items():
            assert len(digest) == 64, f"{name}: {digest!r} is not a sha256"
            assert all(c in "0123456789abcdef" for c in digest), f"{name}: {digest!r} not lowercase hex"


def test_verify_uses_pinned_hash_without_touching_the_network(monkeypatch):
    """The whole point: for the shipped version, verification must not depend
    on a request that can be blocked or poisoned."""
    name = syncthing._archive_filename(config.SYNCTHING_VERSION)
    v = config.SYNCTHING_VERSION
    v = v if v.startswith("v") else f"v{v}"
    expected = SYNCTHING_PINNED_SHA256[v][name]

    def explode(*_a, **_k):
        raise AssertionError("network was used despite a pinned hash being available")

    monkeypatch.setattr(syncthing, "_fetch_official_sha256sums", explode)
    monkeypatch.setattr(syncthing, "_download", explode)

    class _FakeDigest:
        def hexdigest(self):
            return expected

    monkeypatch.setattr(hashlib, "sha256", lambda _d: _FakeDigest())
    syncthing._verify_archive_hash(b"pretend archive", config.SYNCTHING_VERSION)


def test_tampered_archive_still_rejected_against_pinned_hash(monkeypatch):
    monkeypatch.setattr(syncthing, "_fetch_official_sha256sums", lambda _v: {})
    with pytest.raises(SyncthingError, match="hash mismatch"):
        syncthing._verify_archive_hash(b"tampered bytes", config.SYNCTHING_VERSION)


def test_unpinned_version_falls_back_to_signed_fetch(monkeypatch):
    """A non-default version has no pinned entry, so it must still go through
    the signed-sums path rather than being waved through."""
    archive = b"archive bytes"
    name = syncthing._archive_filename("v9.9.9")
    monkeypatch.setattr(
        syncthing,
        "_fetch_official_sha256sums",
        lambda _v: {name: hashlib.sha256(archive).hexdigest()},
    )
    syncthing._verify_archive_hash(archive, "v9.9.9")


def test_unpinned_version_with_unreachable_sums_is_fatal(monkeypatch):
    def boom(_v):
        raise SyncthingError("Refusing to extract an unverified Syncthing binary")

    monkeypatch.setattr(syncthing, "_fetch_official_sha256sums", boom)
    with pytest.raises(SyncthingError, match="Refusing to extract"):
        syncthing._verify_archive_hash(b"bytes", "v9.9.9")


def test_manifest_covers_every_platform_we_build_for():
    """A partial manifest would send some platforms down the network fallback
    while leaving this machine's tests green."""
    v = config.SYNCTHING_VERSION
    v = v if v.startswith("v") else f"v{v}"
    names = set(SYNCTHING_PINNED_SHA256[v])
    for expected in (
        f"syncthing-macos-arm64-{v}.zip",
        f"syncthing-macos-amd64-{v}.zip",
        f"syncthing-windows-amd64-{v}.zip",
        f"syncthing-linux-amd64-{v}.tar.gz",
        f"syncthing-linux-arm64-{v}.tar.gz",
    ):
        assert expected in names, f"{expected} missing from the pinned manifest"
