# Contributing to CATO

Thank you for your interest in contributing to **CATO (Claude Adaptive Token Optimizer)**! We welcome bug reports, feature proposals, documentation improvements, and code contributions.

---

## 🛠️ Development Setup

CATO has **zero external runtime dependencies** — it runs on pure standard library Python (`>=3.10`).

1. **Fork and clone the repository:**
   ```bash
   git clone https://github.com/marcusaguiarmaciel-cmd/cato.git
   cd cato
   ```

2. **Set up a virtual environment:**
   ```bash
   python -m venv .venv
   # On Linux/macOS:
   source .venv/bin/activate
   # On Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   ```

3. **Install CATO in editable mode:**
   ```bash
   pip install -e .
   ```

4. **Run the unit test suite:**
   ```bash
   python -m unittest discover -s tests -v
   ```

---

## 🧪 Testing Guidelines

Before opening a Pull Request:
- Ensure all unit tests pass across all layers:
  ```bash
  python -m unittest discover -s tests -v
  ```
- If you add new parsers, hooks, or algorithms, add corresponding test cases in `tests/test_cato.py`.
- Verify CLI functionality manually:
  ```bash
  cato doctor
  cato stats
  cato statusline
  cato repomap --budget 600
  ```

---

## 📌 Code Style & Principles

- **Zero-Dependency Philosophy:** The core CATO engine must not require mandatory third-party pip packages. Keep execution latency under 10ms for all hooks.
- **Fail-Open Architecture:** If an unhandled exception occurs inside a hook, CATO must fail open (return exit code 0 and pass-through original tool input) rather than blocking Claude Code execution.
- **Cache-Preserving Integrity:** Optimizations must operate strictly *before* inference and preserve Anthropic prompt caching prefixes.

---

## 📬 Submitting a Pull Request

1. Create a feature branch (`git checkout -b feature/my-enhancement`).
2. Commit your changes with clear, descriptive commit messages.
3. Push to your fork (`git push origin feature/my-enhancement`).
4. Open a Pull Request against the `main` branch.
5. Provide a summary of the change and include test results in your PR description.
