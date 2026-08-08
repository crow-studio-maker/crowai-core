# Release process

CrowAI has two deliberately different validation policies. A **source tree/source bundle** may contain reviewed Chat/Code/Agent V1.0 Python package source. A **Core release** is a Core-only end-user archive and must not contain model-package implementations. Both policies reject private runtime/model binaries, secrets, caches, user/runtime data, databases, logs, and suspicious absolute developer paths.

## Source checkout gate

From a clean checkout:

```bash
python tools/validate_release.py --source-tree .
```

The direct validator is self-non-contaminating: it transparently re-execs itself with interpreter-level `-B` before importing CrowAI release helpers. It therefore does not create `tools/__pycache__` and then reject its own output. This does **not** ignore contamination that already exists; pre-existing `__pycache__`, `.pyc/.pyo`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, coverage files, runtime state and other forbidden source artifacts still fail. A tree intentionally dirtied by `compileall` should fail this direct strict check.

`--source-bundle` applies the same source policy to a ZIP. The validator intentionally permits reviewed `models/<mode>/v1.0/` source under these policies, while rejecting `.gguf`, `.exe`, `.dll`, `.so`, `.dylib`, private DBs and runtime data.

## Core-only release

```bash
python tools/build_release.py
python tools/build_release.py --output /tmp/CrowAI-Core-4.2.0.zip
python tools/validate_release.py --core-release dist/CrowAI-Core-4.2.0.zip
```

`build_release.py` parses its CLI strictly: unknown arguments fail with argparse exit code 2, `--output` accepts an explicit `.zip` destination, parent directories may be created, and an existing directory is never silently overwritten. To prevent a previously generated ZIP from being re-enumerated as source on the next build, any output path inside the repository is restricted to `dist/` (which is excluded from the release source set); destinations outside the repository remain supported. Artifact bytes are deterministic regardless of the permitted output path, and validation occurs before the temporary build is atomically moved into place.

The deterministic builder excludes tests, CI metadata, private runtime directories, generated caches, stale ZIP artifacts, and the versioned model-package implementations. Release input is regular files only: source symlink files/directories are rejected before any bytes are read, so a link inside the checkout cannot import content from outside the repository. ZIP validators also reject Unix symlink entries from `external_attr`. File modes are policy-owned instead of inherited from the host checkout: normal files are emitted as `0644` and only the explicit launcher `run_linux.sh` is emitted as `0755`. Core validation checks not only manifest bytes but canonical ZIP metadata as well: Unix `create_system`, regular-file type/mode, the fixed 1980-01-01 timestamp and deflate compression must match builder policy. It validates the produced ZIP with the strict Core-release policy before reporting success.

Release privacy scanning applies secret-assignment/token checks across recognized source and text formats (including Python, JavaScript/TypeScript, shell and PowerShell), not only configuration files. Python source additionally receives AST-based checks for nested literal/default credentials such as a secret variable or `os.getenv(..., "literal-default")` fallback that a line-oriented regex can miss. This is a conservative release guard, not a replacement for repository/provider secret-scanning controls.

## Required release evidence

The preferred sequence is `python tools/final_verify.py`. It stages/validates clean source first, then runs static/tooling checks, pytest/coverage and compile checks, followed by deterministic Core builds and semantic/tamper validation. This avoids making manual command order part of release correctness.

Individual commands remain useful for debugging. If they are run manually, validate the working source tree before any command that may create checkout-local caches; build Core only after source validation; then validate the exact Core artifact that will be published.

GitHub Actions is stored at `.github/workflows/ci.yml` and mirrors this order in its `release-safety` job. Pytest plugin auto-discovery is disabled in CI/final verification and only the explicitly required coverage plugin is loaded, preventing unrelated host/runner plugins from changing teardown behavior or release evidence. The test matrix covers Python 3.11/3.12/3.13 and runs coverage with branch measurement enabled and enforces a 68% branch-aware total coverage.py gate. Dependency auditing is advisory on pull requests but blocking on push/release-oriented runs; an audit that did not execute must never be reported as passed. Developer test fixtures must not embed realistic absolute private paths as literal release text; fixtures that test sanitization construct such strings at runtime.

Before any public release that is intended to grant open-source reuse rights, the owner must also choose and add a real `LICENSE`; `LICENSE_SELECTION.md` records that this decision is still open.

### Supply-chain reproducibility boundary

CrowAI's own source/Core archive bytes are deterministic under the release policy above. V1.0 also exact-pins direct runtime/development Python requirements and the Python build backend, pins GitHub Actions to owner-verified commit SHAs, selects an explicit Ubuntu runner generation, pins the CI/runtime Python container to an official Docker Hub index digest, and pins the CI pip bootstrap version. This materially reduces supply-chain drift. A remaining boundary is that transitive Python wheels are not yet hash-locked per platform, so the repository does **not** claim fully hermetic, bit-for-bit dependency installation across every OS/architecture.

## One-command closing gate

`python tools/final_verify.py` is the strict release-candidate orchestrator. It first stages and validates a temporary clean source representation, then runs checks that may generate local caches. Every child process has a command-appropriate timeout, timed-out process groups are terminated where practical, and an overall deadline defaults to 1800 seconds. Process execution facts are separate from semantic check states: an exit-zero real-model smoke that reports `missing_local_files` is streamed and recorded as `SKIPPED`, never briefly as `PASS`. The final sanitized evidence file is `dist/verification-summary.json`; human output and JSON use the same final semantic status. The report omits private local paths, usernames, secrets, machine identifiers and model-weight paths. The verifier never cleans or deletes the working tree. Missing Ruff, mypy, Node, Flask/Werkzeug or another required dependency is a failure in default strict mode. The explicit `--allow-missing-tooling` / `--allow-missing-runtime-deps` switches exist only for constrained review environments and must not be cited as CI/release PASS evidence.

The automatic verdict is deliberately capped at `RELEASE CANDIDATE`. Even if Docker isolation and real-model smoke are required in a local invocation and both return `PASS`, `final_verify.py` does not promote itself to `READY FOR PORTFOLIO V1.0`, because it cannot attest that the separate GitHub-hosted Python matrix actually passed. Promotion to that portfolio-ready label is a manual release decision after the GitHub Actions, Linux Docker-isolation, and real package-local GGUF smoke evidence are reviewed together.

The Core release never contains `PACKAGE_MANIFEST.json`; that file is source-distribution metadata only. `RELEASE_MANIFEST.json` intentionally excludes itself from its `files` list to avoid a self-hashing cycle, and every other regular Core file must match its recorded byte size and SHA256. Any covered post-build modification must fail validation.

For a source release maintainer change, `python tools/update_source_manifest.py` recomputes source file records. It does not waive privacy checks; always follow it with the strict final verifier.
