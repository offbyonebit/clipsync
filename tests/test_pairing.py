"""Tests for device ID normalization with Luhn checksum validation."""

from __future__ import annotations

import pytest

from clipsync import pairing

# A real device ID derived from a generated cert.pem (valid Luhn checksums).
_VALID_DEVICE_ID = "T2ZW3LO-EJ72MIJ-XGQT2F6-XVHZDDQ-N2NA2O3-XIMRQLU-46PDBL4-TBHO5AA"


def test_normalize_valid_device_id() -> None:
    assert pairing.normalize_device_id(_VALID_DEVICE_ID) == _VALID_DEVICE_ID


def test_normalize_strips_whitespace_and_uppercases() -> None:
    assert pairing.normalize_device_id("  " + _VALID_DEVICE_ID.lower() + "  ") == _VALID_DEVICE_ID


def test_normalize_handles_internal_whitespace() -> None:
    spaced = " ".join(_VALID_DEVICE_ID)
    assert pairing.normalize_device_id(spaced) == _VALID_DEVICE_ID


def test_normalize_rejects_empty() -> None:
    assert pairing.normalize_device_id("") is None
    assert pairing.normalize_device_id(None) is None  # type: ignore[arg-type]


def test_normalize_rejects_too_short() -> None:
    assert pairing.normalize_device_id("ABCDEFG-ABCDEFG") is None


def test_normalize_rejects_invalid_chars() -> None:
    # Base32 alphabet is A-Z2-7; 1, 0, 8, 9 are not valid.
    bad = _VALID_DEVICE_ID.replace("T", "1")
    assert pairing.normalize_device_id(bad) is None


def test_normalize_rejects_typo_with_bad_luhn_checksum() -> None:
    """Same shape, valid base32, but the Luhn check character is wrong.

    Without checksum validation this would slip through to Syncthing
    and be rejected later with a vaguer error.
    """
    # Flip the last char to break the final Luhn check.
    last_char = _VALID_DEVICE_ID[-1]
    bad_char = "B" if last_char != "B" else "C"
    typo = _VALID_DEVICE_ID[:-1] + bad_char
    assert pairing.normalize_device_id(typo) is None


def test_normalize_rejects_typo_in_data_block() -> None:
    """A typo in any of the 13-char data blocks breaks its Luhn check."""
    # Mutate a middle character (not the check char) of the first block.
    typo = "A" + _VALID_DEVICE_ID[1:]
    assert pairing.normalize_device_id(typo) is None


@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_validate_luhn_checksums_each_block(index: int) -> None:
    """Corrupting any of the 4 blocks must fail validation."""
    raw = _VALID_DEVICE_ID.replace("-", "")
    # Each block is 14 chars: 13 data + 1 check. Corrupt the check char.
    block_start = index * 14 + 13
    original_check = raw[block_start]
    # Pick a different valid base32 char as the corrupted check char.
    new_char = next(c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" if c != original_check)
    corrupted = raw[:block_start] + new_char + raw[block_start + 1 :]
    corrupted_id = "-".join(corrupted[i : i + 7] for i in range(0, 56, 7))
    assert pairing.normalize_device_id(corrupted_id) is None


def test_pair_with_device_rejects_invalid_id() -> None:
    from clipsync.syncthing import SyncthingClient

    client = SyncthingClient(api_key="x", base_url="http://127.0.0.1:0")
    with pytest.raises(ValueError):
        pairing.pair_with_device(client, "not-a-device-id")


def test_accept_pending_device_rejects_invalid_id() -> None:
    from clipsync.syncthing import SyncthingClient

    client = SyncthingClient(api_key="x", base_url="http://127.0.0.1:0")
    with pytest.raises(ValueError):
        pairing.accept_pending_device(client, "not-a-device-id")
