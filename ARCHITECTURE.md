# Architecture summary

The canonical architecture document is [`docs/architecture.md`](docs/architecture.md).

CrowAI Core 4.2 uses an application factory, Flask Blueprints, services for business rules, repositories for SQL, numbered SQLite migrations, server-side sessions, a model service/registry boundary, and a rebuildable per-user JSON snapshot cache. `app.py` remains a thin compatibility WSGI entry point.

## V1.0 model packages

Chat, Code, and Agent remain separate packages under `models/<mode>/v1.0/`. Their answering runtime is version-local: configuration is resolved from the version directory, GGUF/mmproj files live under `model/`, llama.cpp executables and native libraries live under `runtime/`, and prompt/provider files stay inside the package. The engines do not walk to the project root and do not reuse an unrelated inference server already listening on the configured port.

Core owns authentication, persistence, upload lifecycle, public-result sanitization, and model discovery. A package receives only the request data it needs. For file analysis Core can pass a private, validated transient upload path; it is request input, not a model dependency, and is removed before API output.

Agent V1.0 adds multimodal/file inspection and structured product evidence/cards. Code V1.0 provides local project-aware generation, review, editing, syntax validation, and one repair pass without web access. Chat V1.0 keeps its direct conversation path while using the same version-local inference isolation.

The existing Settings UI and `/api/settings` contract are frozen and protected by snapshot/contract tests.

## Stabilized lifecycle boundaries

Core owns durable conversation memory, idempotency leases, upload/request limits and public result sanitization. Model discovery performs static callback/config/capability validation before selected package import; development reload detection hashes package content rather than relying only on timestamps. Package health output is recursively sanitized.

Chat explicitly budgets its 4096-token context and remains offline. Code can optionally validate generated Python through a constrained subprocess runner but does not claim sandbox isolation. Agent owns its provider/fetch lifecycle when it requests network access, avoiding duplicate generic Core search; it retains stable source provenance/evidence IDs and bounded package-local cache/session state.
