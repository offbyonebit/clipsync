# Contributing to ClipSync

## Dev environment

```bash
git clone https://github.com/offbyonebit/clipsync
cd clipsync
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Running checks

```bash
# Lint + format
ruff check clipsync/ tests/
ruff format clipsync/ tests/

# Type check (winreg resolves fully on Windows; Linux skips it via ignore_missing_imports)
mypy clipsync/

# Tests (no display required — clipboard access is faked in tests)
pytest tests/
```

All three must pass before opening a PR. CI enforces the same checks on every pull request.

## Code style

- **Formatter:** Ruff (line-length 120, `py311` target). Run `ruff format` before committing.
- **Types:** mypy strict-ish (`check_untyped_defs`, `no_implicit_optional`, `warn_unused_ignores`). All new functions need type annotations.
- **Comments:** Only when the *why* is non-obvious. Docstrings are fine for public APIs; skip them for internal helpers.

## Making changes

1. Fork and create a branch off `main`.
2. Keep commits focused — one logical change per commit.
3. Add or update tests for any behaviour change.
4. Open a PR against `main`. Describe *what* changed and *why*.

## Architecture overview

See [`CLAUDE.md`](CLAUDE.md) for a module-by-module breakdown of how ClipSync works.
