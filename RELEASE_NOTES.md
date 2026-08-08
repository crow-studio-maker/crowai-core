# CrowAI Core 4.2.0

## Final V1.0 hardening

- Direct source validation is now self-non-contaminating at interpreter startup: the documented `python tools/validate_release.py --source-tree .` command transparently re-execs with `-B` before project-local imports while still rejecting pre-existing `__pycache__`, `.pyc/.pyo`, test/type-check caches and other forbidden source contamination.
- `tools/final_verify.py` now stages/validates clean source before cache-producing tooling, separates raw process outcomes from semantic check outcomes, and never streams a false PASS for a real-model/Docker check that later parses as `SKIPPED`.
- The canonical verifier now writes sanitized machine-readable evidence to `dist/verification-summary.json`; the JSON and human summary share the same final semantic status model and omit private local paths/commands.
- Private runtime configuration now rejects lexical symlink/junction/reparse-point components before path resolution, and custom populated production roots require a validated CrowAI ownership marker before any recursive permission hardening.
- Python secret scanning no longer mistakes `os.getenv("SECRET_NAME")` selector strings for embedded credentials; only direct literals or actual fallback/default literals are treated as secret values.
- Docker execution evidence now truthfully reports that its bounded tmpfs `/workspace` is writable by the configured unprivileged container user.
- Trusted-local validation starts Python with `-E -s -S`, removing global `sitecustomize`/site-package startup hooks from this development-only backend while preserving workspace-local imports.
- App-factory configuration is now security-normalized: production-derived cookie/permission/request-limit settings cannot be weakened by late programmatic overrides, numeric overrides share environment bounds, and a programmatic production selection prevents `.env` loading before configuration is built.
- Core ZIP validation now covers canonical metadata (Unix origin, regular-file mode, deterministic timestamp and compression) in addition to manifest SHA-256/size integrity, so permission-only archive tampering is rejected.
- Python release secret scanning now uses AST checks for nested literal/default credentials such as `PASSWORD = os.getenv("PASSWORD", "...")`.
- The branch-aware CI/final-verifier coverage gate is raised from 65% to 68% after targeted security/release regression tests.
- Release output safety now rejects repo-local `--output` paths outside `dist/` and excludes stale `.zip` artifacts from Core source enumeration, preventing previous custom releases from contaminating later builds.
- Nested SQLite paths under the private instance root now harden every CrowAI-owned intermediate directory to `0700`; strict production rejects database paths outside that root. Private permission helpers reject symlink roots instead of chmod-following them.
- Production model roots are now read-only inputs: a model directory may be read-only or entirely absent, and CrowAI no longer requires model-package write access.
- Development model reload now performs a bounded old-package `shutdown()` before module-cache purge; failed/timed-out shutdown blocks reload instead of orphaning a package-owned llama.cpp process.
- Docker Code execution no longer uses a writable host workspace. Prepared host input stays private and read-only in the container; generated writes go to a hard-capped `/workspace` tmpfs. CrowAI mirrors an unprivileged host UID/GID (or hands a root-launched input tree to 65534).
- Private POSIX runtime data is now explicitly hardened: runtime directories `0700`, database/WAL/SHM/uploads/user snapshots `0600`, with existing broad modes tightened at startup and production fail-closed behavior.
- `tools/final_verify.py` now gives every child process an explicit timeout, terminates timed-out process groups where practical, flushes progress, records timeout/duration evidence, and enforces an overall verifier deadline.
- `tools/build_release.py` now uses strict argparse handling, supports deterministic `--output PATH` builds, rejects unknown options/directories clearly, validates before atomic publication, and no longer ignores CLI arguments.
- Docker Code execution now uses a read-only host input mount plus a hard-capped writable `/workspace` tmpfs, alongside core-dump disablement and individual-file limits.
- Core release metadata is now artifact-truthful: source `PACKAGE_MANIFEST.json` is excluded from Core and `RELEASE_MANIFEST.json` is the Core authority.
- Source/Core manifest validators now enforce exact file coverage, byte sizes and SHA-256 hashes; post-build tampering is rejected.
- Runtime readiness/startup now share one package-local cross-platform resolver, including Windows `.exe`/Linux extensionless parity, POSIX executable checks and GGUF placeholder rejection.
- The legacy public trusted-local Python helper was removed; Docker isolation gained unprivileged execution, IPC/open-file limits, `--pull never`, and digest-pinned image support/documentation.
- Added `tools/final_verify.py` as the single closing verification path and added scoped mypy plus deterministic Core verification to GitHub Actions.
- Real-model smoke now has mode-specific evidence checks when private files are present; source-only runs remain truthfully `SKIPPED`.
- GitHub Actions now lives at the auto-discovered `.github/workflows/ci.yml`; the obsolete `github/` path is rejected by tests.
- Model readiness now distinguishes installed package source from runnable local models. Source-only checkouts expose diagnostics but cannot select/submit an unavailable model, and health is degraded when zero models are runnable.
- Code Python execution now has explicit backend trust boundaries: default disabled, Docker-isolated execution when explicitly requested and available, and a double-opt-in trusted-local development path that is never described as a sandbox.
- `requirements.txt` and `pyproject.toml` now agree on PyMuPDF and are checked for drift.
- Risk-focused engine/planner/vision/Code-tool tests raised measured branch-aware coverage to the V1.0 68% gate in the available non-Flask validation environment.
- Fixed XML validation reporting in Code: `ElementTree.ParseError` is handled before its `SyntaxError` base class so malformed XML is no longer mislabeled as a Python syntax error.

## Added

- Application factory and isolated runtime extensions
- Blueprint-based workspace, auth, conversation, upload, settings, user, and system routes
- Service/repository boundaries and typed conversation request schemas
- Numbered SQLite migrations and request idempotency ledger
- Model SDK, package template, and package validator
- GitHub Actions, contribution guide, release builder, and privacy validator
- Structured request logging and database health reporting

- Clarified the final-verifier verdict contract: `RELEASE CANDIDATE` is intentionally the highest automatic local verdict, while `READY FOR PORTFOLIO V1.0` requires manual review of external GitHub CI, Linux Docker isolation, and real GGUF smoke evidence.

## Hardened

- Server-side sessions and session key allowlist
- CSRF/origin validation and login/register rate limiting
- Upload atomic writes, arbitrary-extension passive inspection, MIME/signature sniffing, and archive expansion/compression limits
- Model result sanitization and local-path removal
- Development secret permissions and production configuration validation
- User snapshot files written atomically with private permissions

## Preserved

- Drag-and-drop uploads
- Per-user `users/user <username>/` snapshots
- F5/server-restart persistence for settings, drafts, uploads, and conversations
- Username-based page and conversation URLs
- Existing Settings markup, labels, fields, route behavior, and schema
## Fixed in this package

- Destructive confirmation dialogs contain no username or conversation-name field
- Closing or cancelling a delete confirmation can no longer submit the delete action
- Sign-out uses a local CrowAI confirmation only; it does not request a username or invoke Google authentication
- The message textarea has its native focus border and appearance disabled, with a new static asset cache version
- Rename remains available in its own dedicated dialog



## V1.0 mode package upgrade

- Agent V1.0 now uses the configured Qwen3-VL model/projector entirely from its own version directory, performs local multimodal/document inspection before deciding whether web access is needed, understands short follow-up product queries, normalizes commerce data, and exposes safe product cards.
- Agent file inspection handles renamed/signature-detected files, office containers with embedded images, image-only PDF visual fallbacks, archives/compressed data, and printable strings from unknown binaries without executing uploads.
- Code V1.0 now uses the configured Qwen2.5-Coder model from its own version directory with a larger context window, project-aware task routing, multi-file generation, safe relative paths, syntax validation/repair, better test-file naming, and stronger project archive inspection.
- Chat V1.0 behavior remains direct/offline while its runtime/model path isolation and owned-process verification are hardened.
- All three V1.0 engines reject package path escape and unrelated inference processes; no V1.1 package is introduced.
- Package-local runtime/model placeholder directories and `LOCAL_FILES.md` document the exact private binaries that must be supplied locally.

## V1.0 stabilization pass — 2026-08-08

- Split release validation into explicit source-tree/source-bundle and Core-only policies. Reviewed V1.0 package source is legal in source distributions, while Core releases still reject all model-package implementations and both policies reject weights, native runtimes, private data, caches and secrets.
- Added deterministic Core release CI ordering: full dependency install, Flask/Werkzeug import proof, formatting/Ruff/package/compile/JavaScript/pytest checks, source-tree validation, Core build and Core ZIP validation.
- Local `.env` is loaded before Core configuration without overriding process variables; all local entry points default to `127.0.0.1`. Upload per-file and total-request limits are now separate.
- Added durable bounded `conversation_memory` with structured fact provenance, correction handling, mode state, deletion cascade and owner isolation. Added stale idempotency lease recovery and periodic stale-ledger maintenance.
- Agent V1.0 now intentionally reads bounded package session state as fallback, deletes it with conversation cleanup, prunes page/product/session caches during normal lifecycle, removes unused global cancellation state, preserves search provider/query/source provenance, assigns stable source IDs and reports evidence-derived quality/limitations.
- Agent network hardening now covers private/local IPv4/IPv6/DNS targets, redirects, final URL revalidation, robots policy, bounded downloads, prompt-injection warning signals and deterministic mocked search/fetch flows.
- Chat V1.0 remains offline and feature-frozen. Its 12,000-character Core input contract no longer has a hidden ~5,000-character truncation; a 4096-token-aware budget keeps the current request intact, drops oldest raw history before durable memory, dynamically reserves output space and returns a clear non-generation error if the current request itself cannot fit.
- Code V1.0 keeps automatic syntax validation separate from execution. Generated Python execution is disabled by default, explicit isolated execution uses an ephemeral Docker backend with host-filesystem/network isolation when available, and a separately named trusted-local development backend requires double opt-in and remains explicitly unsafe for untrusted code.
- Code artifacts now carry stable create/update operation metadata and bounded unified diffs for updates; Windows drive/stream markers, traversal, absolute paths and case-insensitive collisions are rejected.
- Model registry scanning statically validates required callbacks before selected package import, enforces package-local configured files, recursively sanitizes health output and uses content fingerprints for deterministic development reload.
- No new mode or V1.1 package was created. The Settings UI/API contract remains unchanged.
