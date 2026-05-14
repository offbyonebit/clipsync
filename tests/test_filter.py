"""Tests for the free-version filter module (no-op)."""

from __future__ import annotations

import pytest

from clipsync.filter import check


def test_check_always_allows():
    """Free version always returns False (allows all content)."""
    assert check("anything at all") is False


def test_check_with_patterns_ignored():
    """Patterns are ignored in free version."""
    assert check("secret", patterns=["secret"]) is False


def test_check_with_settings_ignored():
    """Settings are ignored in free version."""
    settings = {"sync_filter_patterns": ["sensitive"]}
    assert check("sensitive content", settings=settings) is False
