# CrowAI V1.0 final release-candidate validation report

Validation date: 2026-08-08

## Scope

This closing pass is limited to correctness, release truthfulness, portability, verification and security footguns. It does not create V1.1, add a mode, redesign Settings, or introduce unrelated UI/product features.

The source distribution contains reviewed Chat/Code/Agent V1.0 implementation source but intentionally contains no private GGUF weights or llama.cpp native runtime binaries. The Core-only release is a different artifact and intentionally excludes the three mode implementation packages.

## Closed P0 findings

### Core artifact truthfulness

`PACKAGE_MANIFEST.json` is source-distribution metadata and is excluded from the Core ZIP. The Core artifact is described only by `RELEASE_MANIFEST.json`, which declares `artifact_type: core-release` and explicitly states that model packages, model binaries, native runtime binaries and runtime user data are not included. Core validation compares those claims with actual archive contents.

### Release integrity

Source and Core manifests are now semantic integrity contracts rather than descriptive lists. Validation requires the supported schema/artifact type, unique safe paths, exact file coverage, exact byte sizes and exact SHA-256 values. The manifest file itself is intentionally excluded from its own `files` list to avoid a self-hash cycle. A regression test mutates Core `README.md` while preserving the old manifest and requires validation failure.

### Cross-platform runtime readiness

Registry readiness and all V1.0 engines share `models/local_files.py`. A configuration naming `runtime/llama-server.exe` accepts only that package-local path or the extensionless package-local sibling; an extensionless configuration likewise permits the `.exe` sibling. No system `PATH`, project-root or other-package fallback exists. POSIX runtime candidates must be executable. Model/projector candidates must be non-empty GGUF files with the `GGUF` magic header. Public readiness distinguishes missing from invalid categories without exposing filesystem paths.

## Private local data permissions

CrowAI now treats local runtime state as private by construction. On POSIX, configured private runtime directories (`instance/`, `users/`, `uploads/` and per-owner upload directories) are hardened to `0700`, while the SQLite database and sidecars, uploads and upload `.part` files, user JSON and generated local secrets are hardened to `0600`. Existing broad modes under those explicitly configured private roots are tightened during startup without recursively chmodding source/model packages. Production POSIX permission failures fail closed; development reports a warning where the filesystem cannot apply the requested mode. Windows remains functional, but full ACL enforcement is explicitly a deployment/OS responsibility rather than a claim made from POSIX-style chmod.

A direct POSIX probe after the change created `instance/` as `0700`, `workspace.db` as `0600`, and SQLite `-wal`/`-shm` sidecars as `0600`. Upload tests observe the temporary `.part` file while it is open and require both it and the final uploaded file to be `0600`. User-supplied private paths are now inspected before canonical resolution; a symlink/junction/reparse-point in the configured path fails before recursive chmod can touch its target. In production, custom private roots are claimed only when new/empty (apart from `.gitkeep`) or when a validated `.crowai-private-root` marker proves prior CrowAI ownership; populated unrelated directories fail before their modes are changed.

## Last-mile runtime/release hardening

Core release output is now contamination-safe. A `--output` destination inside the repository is accepted only under `dist/`, and `.zip` files are excluded from Core source enumeration. This closes the edge case where `custom-one.zip` in the repository could become input to a later `custom-two.zip` build. External output paths remain supported.

Database confidentiality now includes nested paths: when `DATABASE_PATH` is under the configured private `INSTANCE_DIR`, every CrowAI-owned intermediate directory is created/hardened as `0700`, while the DB and SQLite sidecars remain `0600`. Strict/production mode rejects a database path outside that private root. Private permission helpers reject a symlink passed as the root file/directory, so their documented no-follow contract matches behavior.

The production model root is read-only application input. CrowAI no longer creates a missing production model directory and does not require write access to an existing one. Development can still create the conventional directory, but package reload itself only requires readable/traversable source.

Development reload now retires an already loaded package before cache purge: the old module's optional `shutdown()` runs with a bounded wait. If it raises or exceeds the bound, reload/removal is deferred and the old module remains reachable, avoiding the previous orphan-process/port-collision failure mode.

## Code execution boundary

Generated Python execution remains disabled by default. The public application path is `execute_python_artifacts(..., execution_policy=...)`; the old public trusted-local compatibility helper is gone. The preferred isolated backend is an ephemeral Docker container with no network, no IPC namespace sharing, read-only root filesystem, dropped capabilities, `no-new-privileges`, unprivileged UID/GID, PID/CPU/memory/open-file/output/time limits, per-file `fsize` limits, core dumps disabled, a read-only host input mount, a hard-capped writable `/workspace` tmpfs, bounded `/tmp`, and `--pull never`. On POSIX, the prepared host input tree remains private (`0700` directories, `0600` files) and is mounted read-only; CrowAI mirrors the invoking unprivileged UID/GID, or hands a root-launched input tree to UID/GID 65534. Generated writes stay inside the aggregate-size-limited tmpfs instead of a writable host bind mount. If Docker is unavailable, CrowAI does not fall back to host Python.

Docker execution now keeps host input read-only and places writable `/workspace` on a hard-capped tmpfs, so the aggregate workspace limit is actually enforced by the container runtime rather than inferred after execution. Execution evidence now correctly states that the unprivileged container UID/GID can write that bounded tmpfs workspace while the host input remains read-only. `TrustedLocalRunner` remains available only for explicit development/testing under the existing double opt-in and is documented as a constrained host subprocess, not a sandbox. Its interpreter starts with `-E -s -S`, preventing global `sitecustomize`, user-site and unrelated site-package startup hooks from making the development runner/tests host-dependent while retaining workspace-local imports. V1.0 uses an operator-verified immutable Python image digest in both Code configuration and CI.

## Verification and CI

`.github/workflows/ci.yml` is in GitHub's discoverable path. The main matrix installs development/runtime dependencies, proves Flask/Werkzeug imports, runs dependency/format/Ruff/mypy/model-package/compile/JavaScript checks, and rejects a pytest report containing the missing-Flask skip reason. A Linux isolation job runs the real Docker sentinel/network test. Release safety runs the one-command final verifier, deterministic Core builds and semantic Core validation. The workflow does not publish release bytes; the owner must publish only an exact artifact that has already passed the release-safety gate.

`python tools/final_verify.py` stages a clean source representation without deleting or silently cleaning the working tree, validates source manifest/privacy, builds a wrapped source bundle, runs the test/tooling gates, builds Core twice and compares bytes/hashes, verifies Core manifest semantics, runs the tamper regression, and reports optional Docker/real-model evidence separately. Every spawned verification child has an explicit command-appropriate timeout; the verifier also has a configurable overall deadline (default 1800 seconds), starts child process groups where practical, terminates timed-out process trees, emits flushed start/finish progress, classifies timeout evidence as `TIMEOUT`, and records `duration_ms` plus `timeout_seconds` in JSON. A required timeout fails the verdict. Explicit `--allow-missing-*` switches exist only for constrained review environments and are not equivalent to CI evidence.

`tools/build_release.py` now has a strict argparse CLI. Both the default build and `--output PATH` are supported, unknown options fail with argparse exit code 2, directory destinations are rejected, parent directories may be created, and the artifact is first built/validated in a temporary file before atomic replacement. Repo-local output is restricted to `dist/` and stale `.zip` artifacts are excluded from Core source enumeration; permitted destination paths do not affect deterministic Core bytes.

## Immutable model packages and active-generation lifecycle

All Chat/Code/Agent V1.0 package inputs are now treated as immutable. Engine logs, Agent SQLite/cache state and optional Code debug captures live under the Core-owned `instance/model_state/<mode>/v1.0/` tree; model packages no longer require write access after installation. POSIX state directories/files use the same private `0700`/`0600` policy as other sensitive runtime state.

Conversation generation is serialized by a persistent operation-wide request lease. A refresh can therefore recover the active state from Core instead of trusting browser-local JavaScript state: initial route hydration remains locked until the server state is known, the UI restores a `Thinking…` placeholder, polls until the turn completes, and disables prompt submission, uploads and model changes for that conversation. Core independently rejects a second concurrent turn with HTTP 409 semantics. Deleting an active conversation keeps the current composer hard-locked while cancellation is in progress, marks the in-process turn cancelled, terminates the package-owned local llama-server process tree without waiting for the serialized request lock, and then waits a bounded interval for the exact request thread to unwind before deleting SQLite. If deletion fails closed because shutdown does not finish in time, the UI re-hydrates the server-side processing lease before it unlocks. Startup publication of a new llama-server process is synchronized with cancellation, and readiness loops observe cancellation, closing races that could otherwise orphan a just-started backend. Late results are not persisted during deletion; a cancellation that cannot unwind within the bound fails deletion closed rather than racing a live database writer. The request lease is periodically renewed during long inference and is released on completion/error/deletion.

## Release input hardening

Release and source-manifest tooling reject symlink inputs before reading them, and ZIP validation rejects Unix symlink entries encoded through `external_attr`. Source privacy scanning now includes hard-coded secret assignments/tokens in recognized programming/script formats as well as config text. Its Python AST pass distinguishes an environment selector such as `os.getenv("CROWAI_SECRET_KEY")` from credential material and examines only direct literal values or actual fallback/default literals, eliminating the selector-string false positive without reopening nested-default leaks. ZIP file modes come from a fixed release policy (`0644` normal files; `0755` only for explicitly declared executables), rather than checkout executable bits. These checks close external-file symlink exfiltration and host-metadata reproducibility edge cases.

Supply-chain reproducibility is intentionally scoped separately from CrowAI archive determinism. CrowAI now exact-pins its direct runtime/development requirements, pins the Python build backend and pip bootstrap, references the GitHub Actions used by CI by reviewed full commit SHA, uses an explicit Ubuntu runner generation, and pins the Code/Docker CI image by reviewed sha256 digest. The remaining boundary is transitive Python artifacts: wheels/sdists are not yet hash-locked per supported platform, so CrowAI does not claim a fully hermetic cross-platform dependency install. No unverified external SHA/digest is fabricated.

## Evidence source of truth

Numerical test counts, coverage values and Core hashes are intentionally **not hand-maintained in this document**. The canonical command:

```text
python tools/final_verify.py
```

stages/validates clean source before cache-producing tooling and writes sanitized machine-readable evidence to `dist/verification-summary.json`. That artifact is the source of truth for the exact run. It records semantic check states (`PASS`, `FAIL`, `SKIPPED`, `NOT_AVAILABLE`, `TIMEOUT`), pytest counts/coverage when available, deterministic Core SHA evidence, and per-mode real-model smoke status without local filesystem paths, usernames, secrets, machine identifiers or model-weight paths.

A zero process exit code is not sufficient evidence of PASS. In particular, source-only real-model smoke remains `SKIPPED / missing_local_files`, and the streaming `[DONE]` line uses that same semantic state rather than first reporting PASS. The report verdict remains **RELEASE CANDIDATE** until the external GitHub CI, real Docker isolation and real package-local GGUF smoke evidence required by the release policy exists.

The direct debugging command `python tools/validate_release.py --source-tree .` is also self-non-contaminating: it bootstraps into interpreter-level `-B` before project-local imports, but it still rejects bytecode/caches that were already present. A checkout intentionally dirtied by normal `compileall` must therefore fail direct strict source validation; the canonical final verifier avoids this ordering trap by validating a clean staged representation first.

## Local model smoke semantics

`python tools/smoke_models.py` performs static readiness first. Without the package-local private GGUF/runtime files, Chat, Code and Agent remain `SKIPPED` with missing requirement categories; they are never rewritten to PASS. When private files are installed, the smoke tool requires backend/alias readiness and the existing mode-specific evidence checks.

## Tooling availability in this environment

- `tools/check_format.py`: available and executed.
- Python compileall: available and executed.
- `node --check static/workspace.js`: available and executed.
- Ruff: declared and run by CI, but not installed in this review environment; no local PASS is claimed.
- mypy: declared and run by CI, but not installed in this review environment; no local PASS is claimed.
- Flask/Werkzeug: not installed in this review environment; no local integration PASS is claimed here.
- Docker: not available here; real isolation is not claimed as locally executed.
- GGUF/llama.cpp private assets: not present; real model inference is not claimed.

## Known security limitation

Agent URL validation blocks local/private destinations, validates DNS answers and redirects/final URLs, and treats fetched content as untrusted evidence. DNS validation and the later socket connection are not cryptographically pinned to one resolution, so DNS rebinding/TOCTOU cannot be completely excluded at application level. Host/container/network egress controls remain the stronger boundary for hostile deployments.

## Version and license clarity

CrowAI product release is **V1.0**. Core runtime/protocol package version is **4.2.0**. Public model package versions are **v1.0**; `minimum_core_version` is compatibility metadata, not product marketing versioning.

No license is selected automatically. `LICENSE_SELECTION.md` records the owner's pending decision. Source-visible code is not described as open source unless the owner intentionally adds a license granting those rights.

## Verdict boundary

The source can be called a **release candidate** after the local source/Core validators and deterministic build checks pass. A stronger statement that the deployed V1.0 is fully proven still requires real GitHub Actions evidence, a real Docker isolation PASS on Linux, and real GGUF/llama.cpp smoke results from the owner's package-local installations. SKIPPED/NOT_AVAILABLE evidence must never be relabeled PASS.
