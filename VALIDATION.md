# Validation

## Final V1.0 verification

From a checkout with the declared development dependencies installed, the canonical release-readiness command is:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python tools/final_verify.py
```

On Windows, activate the virtual environment with the appropriate `Scripts` command. The verifier stages and validates clean source **before** running tooling that may generate local caches, never deletes or silently cleans the working tree, and writes sanitized evidence to `dist/verification-summary.json`. Required `FAIL`, `TIMEOUT`, `NOT_AVAILABLE`, or `SKIPPED` states fail the strict verdict according to policy; exit code zero from a child process is not by itself semantic PASS evidence.

The machine-readable report contains statuses/metrics rather than private command output and intentionally omits local paths, usernames, secrets, model-weight paths, and machine identifiers.

The automatic verdict has an intentional ceiling: a successful `final_verify.py` run reports `RELEASE CANDIDATE`, including when local Docker isolation and real-model smoke are configured as required and both pass. `READY FOR PORTFOLIO V1.0` is not emitted by this local tool; it is a manual decision after the GitHub Actions matrix, Linux Docker isolation, and real package-local GGUF smoke evidence have all been reviewed.

## Individual debugging checks

Run these when isolating a failure. If using the strict working-tree source validator, run it **before** commands that intentionally create checkout-local caches:

```bash
python tools/validate_release.py --source-tree .
python tools/check_dependencies.py
python tools/check_format.py
ruff check .
mypy crowai tools
python -m tools.validate_model_package models/chat/v1.0
python -m tools.validate_model_package models/code/v1.0
python -m tools.validate_model_package models/agent/v1.0
python -m compileall -q crowai models tools tests
node --check static/workspace.js
pytest --cov=crowai --cov=models --cov-branch --cov-report=term-missing --cov-fail-under=68
python tools/build_release.py
python tools/validate_release.py --core-release dist/CrowAI-Core-4.2.0.zip
```

`tools/validate_release.py` transparently re-execs a direct invocation with interpreter-level `-B` before importing project-local helpers. You do **not** need to add `-B` yourself, and the documented direct command on a clean tree therefore cannot manufacture `tools/__pycache__`. It still rejects any cache/bytecode that existed before validation. A working tree dirtied by normal `compileall` therefore fails direct strict source validation by design; the canonical verifier avoids this ordering trap by validating a clean staged representation first.

CI installs `requirements-dev.txt`, explicitly imports Flask/Werkzeug, and therefore must execute the Flask auth/session/CSRF tests rather than accepting environment-driven skips. The source and Core-release policies are intentionally different as described in `docs/release-process.md`.

The Core must also be checked in no-model mode and with temporary/mocked model packages. Normal deterministic tests do not require live internet. Agent network/search tests use mocks rather than external providers.

Real GGUF inference tests additionally require the exact model and llama.cpp runtime binaries documented in each `models/<mode>/v1.0/LOCAL_FILES.md`. If those binaries are absent, real model smoke remains `SKIPPED`; it must never be relabeled `PASS`.
