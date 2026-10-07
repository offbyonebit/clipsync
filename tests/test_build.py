"""Packaging regressions that only appear in frozen builds."""

from clipsync import build


def test_build_collects_lazily_imported_keyring_backends() -> None:
    args = build._common_args("ClipSync")
    index = args.index("--collect-all")
    assert args[index + 1] == "keyring"
