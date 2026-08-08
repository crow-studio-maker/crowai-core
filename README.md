# CrowAI Core

CrowAI combines a secure Flask Core with self-contained Chat, Code, and Agent V1.0 package sources. Core provides account and server-side session handling, persistent conversations, drag-and-drop uploads, per-user JSON snapshots, the model-package registry, and the web workspace. Large GGUF weights and llama.cpp runtime binaries are intentionally not committed to source.

## Distribution types

CrowAI V1.0 is published in two deliberately different forms. The **Git/source repository** contains the reviewed `models/<mode>/v1.0/` Chat, Code, and Agent implementations, but no private weights or native runtimes. The **Core-only ZIP** contains Core, registry/SDK and operational documentation only; it does **not** contain those mode implementation directories. `PACKAGE_MANIFEST.json` describes source distributions only, while `RELEASE_MANIFEST.json` is authoritative for a Core-only ZIP.

The application still starts safely when those private binaries are absent. Authentication, Settings, persistent workspace state, user routes, health checks, and the conversation shell remain available; package health reports which local runtime/model files are missing. In production the model root is treated as read-only application input: it may be mounted read-only, and it may be absent entirely when running Core without installed model packages.

## Core vs. model packages

In the Git/source distribution, reviewed V1.0 package source lives at `models/<mode>/v1.0/`. The Core-only ZIP intentionally omits those mode implementation directories. Neither distribution contains the large private GGUF/runtime binaries. Each installed V1.0 package is self-contained for immutable inputs: configured runtime, model/projector, prompts, and provider/site files must resolve inside that package's own version directory. Mutable logs, caches, Agent session/cache SQLite state, and Code debug captures are never written back into the package; Core routes them to the private `instance/model_state/<mode>/v1.0/` tree. The engine will not reuse an unrelated llama.cpp server already listening on the configured port.

Expected private files are documented in each package's `LOCAL_FILES.md`. The configured models are:

- Agent: `models/agent/v1.0/model/Qwen3VL-8B-Instruct-Q4_K_M.gguf` plus `mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf`
- Code: `models/code/v1.0/model/qwen2.5-coder-7b-instruct-q4_k_m.gguf`
- Chat: `models/chat/v1.0/model/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf`
- Runtime: `runtime/llama-server.exe` (or package-local `runtime/llama-server` on Linux) inside every V1.0 package

Installing or replacing package code grants that package Python-code execution privileges. Only use package code you trust and have reviewed. Manifest validation and path-locality checks detect structural mistakes or modification; they do not prove publisher authenticity.

## Features

- Flask application factory and focused Blueprints
- Server-authoritative accounts, CSRF protection, and SQLite sessions
- Session/CSRF rotation on login and registration
- Persistent conversations, unfinished drafts, and bounded durable per-conversation memory
- User routes such as `/<username>`, `/<username>/settings`, and `/<username>/chat/<id>`
- Drag-and-drop and file-picker uploads with random storage names, arbitrary-extension passive inspection, and archive safety limits
- Agent V1.0 local multimodal analysis for images, PDFs, office documents, archives, compressed files, source/text files, and unknown binaries
- Agent product lookup/normalization with product cards, images, prices, ratings, sellers, availability, and source links
- Code V1.0 project-aware generation/edit/review/debug routing, multi-file planning, automatic syntax validation/repair, and explicit-policy Python execution with an isolated Docker backend when available
- Package-local llama.cpp process ownership and model-alias verification for Chat, Code, and Agent V1.0
- `users/user <username>/` JSON snapshots for settings, drafts, uploads, and conversations
- Manifest-driven model discovery with a public SDK
- Safe no-model mode and degraded health reporting
- Deterministic release builder and privacy validator
- Automated tests and GitHub Actions

SQLite is the authoritative data source. Files under `users/user <username>/` are private, atomic, rebuildable snapshots; they never contain passwords or session IDs and are excluded from Git and release archives.

## Architecture

```text
Browser
  ↓
Flask Core
  ├─ Auth / SQLite Sessions / CSRF
  ├─ Conversations service + repository
  ├─ Upload service + safe inspection
  ├─ Settings (frozen contract)
  ├─ User JSON snapshot cache
  └─ Model service
       ↓
  Model Registry
       ↓
models/<mode>/<version>/
```

See [`docs/architecture.md`](docs/architecture.md) for the full module map and ownership boundaries.


## Versioning

CrowAI's product release is **V1.0**. The Core runtime/protocol Python package is **4.2.0**, while public model-package versions are **v1.0**. A package's `minimum_core_version` is a compatibility requirement for the Core protocol/runtime; it is not the product marketing version.

## Requirements

- Python 3.11 or newer
- Writable private runtime directories (`instance`, `uploads`, `users`); mutable model state lives below `instance/model_state`, while the model root may be read-only or absent in production
- A strong stable `CROWAI_SECRET_KEY` in production

## Quick start

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
cp .env.example .env
python app.py
```

Open `http://127.0.0.1:5000`.

For a production-style local server:

```bash
python serve.py
```

## Configuration

For local development, CrowAI loads the project `.env` before configuration is constructed and never overrides an explicit process environment variable; a programmatic `create_app({"ENVIRONMENT": "production"})` selection also suppresses `.env` loading. All local entry points default to `127.0.0.1`; LAN exposure requires an explicit `CROWAI_HOST=0.0.0.0` operator choice. Production refuses to start without a strong secret, disables debug mode, requires secure cookies, and enables HSTS headers. `CROWAI_MAX_UPLOAD_BYTES` is a per-file limit while `CROWAI_MAX_REQUEST_BYTES` controls the whole HTTP request. See [`docs/configuration.md`](docs/configuration.md).

## Running without models

Missing-binary/no-runnable-model mode is intentional. CrowAI distinguishes an **installed package** (validated V1.0 Python source) from a **runnable model** (all required package-local runtime/model/projector files are present):

- `/health` returns `status: "degraded"`, `core_ready: true`, and `models_available: false` when zero models are runnable
- `/api/bootstrap` keeps installed packages diagnostically visible, returns `runnable_models: []`, and leaves `default_model` empty
- installed-but-unavailable models can be shown disabled as `Local files missing`; the composer cannot submit against them
- model-dependent creation/upload calls return a safe `MODEL_UNAVAILABLE` error
- bootstrap/readiness checks are static and do not start llama.cpp merely to render the UI or health endpoint

## Installing model packages

A package requires `manifest.json` and `__init__.py`, including `prepare_request()` and `finalize_result()` callbacks. CrowAI also validates package-local `*_file` references so V1.0 runtimes/models cannot escape their version directory. Validate a package before installing it:

```bash
python -m tools.validate_model_package /path/to/models/chat/v1.0
```

Read [`models/SDK.md`](models/SDK.md) and [`docs/model-development.md`](docs/model-development.md).

## Testing

```bash
pip install -r requirements-dev.txt
python tools/final_verify.py  # canonical V1.0 release-readiness command
pytest
pytest --cov=crowai --cov=models --cov-branch --cov-report=term-missing
python tools/check_format.py
ruff check .
mypy crowai tools
python tools/smoke_models.py  # optional; real local GGUF/runtime smoke only when files exist
```

Tests use temporary databases and model fixtures. They do not require private model packages, live internet access, real user files, or real email.

### Final V1.0 verification

`python tools/final_verify.py` is the canonical closing gate. It first stages and validates a clean source representation, then runs tooling/tests that may create local caches, performs deterministic Core builds/tamper checks, and writes a sanitized machine-readable report to `dist/verification-summary.json`. It never deletes or silently cleans the working tree. The report uses semantic `PASS`, `FAIL`, `SKIPPED`, `NOT_AVAILABLE`, and `TIMEOUT` states; a subprocess exit code of zero is not treated as PASS when a check's own evidence says it was skipped.

The standalone strict command `python tools/validate_release.py --source-tree .` is still useful when debugging a checkout. A direct invocation transparently restarts under interpreter-level `-B` before importing project-local helpers, so a clean tree stays clean without requiring the operator to remember `-B`, but **pre-existing** `__pycache__`, `.pyc`, test/type-check caches, runtime data, secrets, or other forbidden contamination still fail. If you intentionally ran `compileall` into the checkout, direct strict validation should fail; use the canonical verifier for release evidence because it validates a clean staged representation before later tooling runs.

## V1.0 memory and context behavior

SQLite is authoritative for durable conversation memory. A bounded `conversation_memory` record stores a summary, structured facts with source-message provenance, and compact mode state. Recent corrections replace stale remembered facts, owner checks prevent cross-user reads, and deleting a conversation cascades Core memory and calls the owning package's conversation cleanup hook. Agent's small private model-state session data under `instance/model_state/agent/v1.0/` is bounded/TTL-maintained and is deleted by that hook.

One conversation can have only one active generation. Core holds a persistent processing lease, so refreshing the page restores a server-backed `Thinking…` state and keeps prompt submission, uploads and model changes disabled until the turn is stored. Deleting an active Chat/Code/Agent conversation cancels the exact in-process turn, terminates the package-owned llama.cpp process tree, waits a bounded interval for the request thread to unwind, and only then deletes SQLite state; a cancellation timeout fails closed rather than leaving a hidden live writer. The browser also stays locked throughout that deletion/cancellation handshake.

Chat remains offline and feature-frozen. Its current user message is never silently truncated: the package budgets against the configured 4096-token context, preserves the current request, prefers durable memory over old raw history, and returns a clear non-generation error when the current request itself cannot fit.

Code syntax/AST validation remains automatic, but generated Python execution is **disabled by default** and is never enabled merely because a prompt says “run” or “test”. Actual execution requires an explicit request policy. The normal isolated backend is an ephemeral Docker container with `--pull never`, `--network none`, `--ipc none`, a read-only container root, an unprivileged UID/GID, dropped Linux capabilities, `no-new-privileges`, bounded CPU/memory/PIDs/open-files/output/time, an individual-file size rlimit, disabled core dumps, and exactly one read-only host input bind plus a bounded in-container `/workspace` tmpfs. On POSIX the container mirrors the invoking CrowAI UID/GID when it is already unprivileged; if CrowAI is running as root, the private input tree is handed to UID/GID 65534. Host input stays `0700` with files `0600`, while writable generated state is limited by the tmpfs aggregate size cap and never written back to the host mount. If that backend is unavailable, execution stays disabled rather than falling back to host Python. A separately named `trusted-local` development backend exists only behind a double opt-in and is explicitly a constrained host subprocess, **not a security sandbox**. Execution evidence reports the backend and only the isolation properties actually enforced. The shipped V1.0 configuration pins the isolated Python image by an operator-verified `@sha256` digest and uses `--pull never`; CrowAI never silently substitutes a mutable image.

Agent treats fetched pages as untrusted evidence, keeps provider/query/source provenance and stable source IDs, revalidates redirects/final URLs, blocks local/private targets by default, and performs bounded cache/session maintenance. URL validation materially reduces SSRF risk, but validation and connection are not cryptographically pinned to one DNS resolution; DNS rebinding/TOCTOU remains a documented limitation. Host/container/network isolation remains the stronger deployment control.

## Security

The application uses server-side opaque sessions, synchronizer CSRF tokens, ownership checks, safe error responses, strict security headers, and private release scanning. On POSIX, private runtime roots are hardened to `0700` and private database/upload/user files to `0600`; user-configured runtime paths reject symlink/junction redirection before resolution, and populated custom production roots require CrowAI's ownership marker before recursive hardening. Windows ACLs remain an operator responsibility. Review [`SECURITY.md`](SECURITY.md) and [`docs/security.md`](docs/security.md) before deploying.

## Repository layout

```text
crowai/                    Core application, services, routes, storage
models/                    Registry and public model SDK
static/ templates/         Workspace UI
users/ uploads/ instance/  Runtime-only private data (.gitkeep only in source)
tests/                     Unit, integration, security, and contract tests
tools/                     Release and package validators
docs/                      Architecture, security, deployment, and operations
examples/                  Non-functional model development template
```

## Release validation

CrowAI intentionally distinguishes a source checkout from the Core-only distribution. The source tree may contain reviewed `models/<mode>/v1.0/` Python package implementations; neither policy permits GGUF weights, native runtimes, private data, secrets, caches, or runtime databases.

```bash
# Preferred release-readiness path:
python tools/final_verify.py

# Individual debugging/release subcommands:
python tools/validate_release.py --source-tree .
python tools/build_release.py
python tools/build_release.py --output /tmp/CrowAI-Core-4.2.0.zip
# If the output is inside the repository, it must be under dist/.
python tools/validate_release.py --core-release dist/CrowAI-Core-4.2.0.zip
```

The Core release excludes model-package implementations and tests. See [`docs/release-process.md`](docs/release-process.md).

### Verification verdict policy

`python tools/final_verify.py` is intentionally a **release-candidate** verifier, not an automatic publication authority. Its highest successful verdict is always `RELEASE CANDIDATE`, even when its local Docker isolation and real-model smoke checks are both required and pass. That ceiling is deliberate: one local run cannot prove that the GitHub-hosted Python 3.11/3.12/3.13 matrix and the other external release evidence actually ran successfully. `READY FOR PORTFOLIO V1.0` is therefore a **manual release decision** made only after reviewing the GitHub Actions result together with the Linux Docker-isolation evidence and the package-local Chat/Code/Agent GGUF smoke evidence. A local `RELEASE CANDIDATE` result must never be presented as that external proof.

## Roadmap

- Separate-process isolation for model packages
- Shared/distributed rate limiting and session backends
- Signed model-package distribution
- Administrative observability without logging private content

## License

A Core license has not been selected by the owner. See [`LICENSE_SELECTION.md`](LICENSE_SELECTION.md). Any future Core license applies only to this Core repository unless a model package explicitly states otherwise.
