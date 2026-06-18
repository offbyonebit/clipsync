"""Tests for the Syncthing device-ID derivation and Luhn mod-32 checksum."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from clipsync.syncthing import _LUHN_BASE32, _device_id_from_cert, _luhn32


def test_luhn32_known_values() -> None:
    # Hand-verified values from Syncthing's reference implementation.
    assert _luhn32("AAAAAAAAAAAAA") == "A"
    assert _luhn32("ABCDEFGHIJKLM") == "O"


def test_luhn32_returns_valid_base32_char() -> None:
    # Every output must be a single character from the base32 alphabet.
    for c in _LUHN_BASE32:
        # Build a 13-char data string from the same char to keep it stable.
        result = _luhn32(c * 13)
        assert len(result) == 1
        assert result in _LUHN_BASE32


def test_luhn32_changes_with_input() -> None:
    # Two different inputs must not always produce the same check char.
    results = {_luhn32(_LUHN_BASE32[i] * 13) for i in range(len(_LUHN_BASE32))}
    assert len(results) > 1


def _generate_self_signed_cert(tmp_path):
    """Generate a self-signed cert/key pair and return the cert path."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "clipsync-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    cert_path = tmp_path / "cert.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, cert


def test_device_id_from_cert_matches_syncthing_algorithm(tmp_path) -> None:
    """The derived device ID must match Syncthing's documented algorithm:

    SHA-256 of the DER-encoded cert -> base32 (no padding) -> split into
    4 x 13 chars, append a Luhn mod-32 check char to each -> split into
    8 groups of 7 with hyphens.
    """
    cert_path, cert = _generate_self_signed_cert(tmp_path)

    from cryptography.hazmat.primitives.serialization import Encoding

    der = cert.public_bytes(Encoding.DER)
    digest = hashlib.sha256(der).digest()
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=")
    assert len(b32) == 52

    expected_chunks = [b32[i : i + 13] for i in range(0, 52, 13)]
    expected_with_checks = "".join(c + _luhn32(c) for c in expected_chunks)
    expected_id = "-".join(expected_with_checks[i : i + 7] for i in range(0, 56, 7))

    actual = _device_id_from_cert(cert_path)
    assert actual == expected_id


def test_device_id_from_cert_has_correct_shape(tmp_path) -> None:
    cert_path, _ = _generate_self_signed_cert(tmp_path)
    device_id = _device_id_from_cert(cert_path)
    # 8 groups of 7 base32 chars separated by hyphens = 56 chars + 7 hyphens.
    assert len(device_id) == 63
    assert device_id.count("-") == 7
    raw = device_id.replace("-", "")
    assert len(raw) == 56
    assert all(c in _LUHN_BASE32 for c in raw)


def test_device_id_from_cert_validates_under_pairing_normalize(tmp_path) -> None:
    """A derived device ID must pass our own normalize_device_id check
    (which includes the Luhn checksum validation)."""
    from clipsync.pairing import normalize_device_id

    cert_path, _ = _generate_self_signed_cert(tmp_path)
    device_id = _device_id_from_cert(cert_path)
    assert normalize_device_id(device_id) == device_id


def test_device_id_from_cert_distinct_for_distinct_certs(tmp_path) -> None:
    a, _ = _generate_self_signed_cert(tmp_path / "a")
    b, _ = _generate_self_signed_cert(tmp_path / "b")
    assert _device_id_from_cert(a) != _device_id_from_cert(b)


def test_device_id_from_cert_raises_on_missing_file(tmp_path) -> None:
    with pytest.raises(OSError):
        _device_id_from_cert(tmp_path / "nonexistent.pem")
