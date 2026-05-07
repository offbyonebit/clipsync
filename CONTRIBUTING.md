# Contributing to ClipSync

Thank you for considering contributing to ClipSync! This guide will help you
get started.

## How to Contribute

### Reporting Bugs

Before creating bug reports, please check the existing issues to see if the
problem has already been reported. When creating a bug report, include:

* A clear and descriptive title
* Steps to reproduce the behavior
* Expected vs actual behavior
* Your operating system and Python version
* Any relevant logs or error messages

### Suggesting Features

Feature suggestions are welcome! Before submitting, please:

* Check if the feature already exists or has been suggested
* Explain the use case and why it would be valuable
* Describe how it should work

### Pull Requests

1. Fork the repository
2. Create a new branch for your feature or bug fix:
   ```bash
   git checkout -b feature/your-feature-name
   ```
3. Make your changes
4. Run tests if applicable:
   ```bash
   python -m pytest tests/
   ```
5. Run linting:
   ```bash
   ruff check .
   mypy clipsync/
   ```
6. Commit your changes with clear, descriptive commit messages
7. Push to your fork and submit a pull request

## Development Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/offbyonebit/clipsync.git
   cd clipsync
   ```

2. Create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Run ClipSync:
   ```bash
   python -m clipsync
   ```

## Code Style

* Follow PEP 8 style guidelines
* Use type hints where possible
* Keep functions focused and modular
* Write docstrings for public APIs

## Testing

* Write tests for new functionality
* Ensure existing tests pass
* Test on multiple platforms if possible (Windows, macOS, Linux)

## Questions?

Feel free to open an issue for any questions or discussions about contributing.
