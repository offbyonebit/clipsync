"""Conditional filter module for the free version.

The premium filter engine is available only in the ultra repo.
On the free branch, this module provides a no-op fallback so that
the clipboard.py conditional import never fails, and filtering
simply allows all content through.
"""

from __future__ import annotations


def check(text: str, **_kwargs: object) -> bool:
    """No-op: always allow content through on the free version."""
    return False
