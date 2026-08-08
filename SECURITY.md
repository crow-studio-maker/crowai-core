# Security policy

## Supported versions

Security fixes are applied to the current CrowAI Core release line. Older local copies should be upgraded before reporting behavior that is already fixed in the current branch.

## Reporting a vulnerability

Do not open a public issue for an active vulnerability or include real credentials, user content, private prompts, or exploit data in a public discussion. Contact the project owner through a private channel selected for the eventual repository. A public security contact has not yet been provided; replace this paragraph before a public launch.

Include the affected Core version, configuration, reproduction steps using sanitized data, impact, and any suggested mitigation. Do not test against systems or accounts you do not own or have permission to assess.

## Security model

- Passwords are hashed with Werkzeug scrypt and never stored in browser storage.
- Sessions are server-side SQLite records; the browser receives only an opaque random ID cookie.
- State-changing requests require a synchronizer CSRF token and same-origin validation.
- Resource access is scoped by `owner_key` for conversations, messages, and uploads.
- Uploads are stored under random identifiers, never executed, and inspected with archive limits.
- API/model errors are sanitized and include request IDs; raw exceptions remain in server logs.
- Production requires a strong secret, secure cookies, no debug mode, and HSTS.
- Runtime data is excluded from Git and release archives.

## Local filesystem confidentiality

On POSIX, CrowAI does not rely on umask for private application data: `instance/`, `users/`, `uploads/` and per-owner upload directories are restricted to `0700`; the SQLite database and WAL/SHM files, uploads/temporary parts, local development secret and per-user JSON snapshots are restricted to `0600`. Existing broader modes are tightened during startup, and production fails closed if the requested POSIX protection cannot be enforced. User-supplied private runtime paths are inspected lexically before resolution; symlink, junction, and Windows reparse-point components are rejected before any recursive chmod can reach their targets. Custom production runtime roots must be new/empty (a source `.gitkeep` is tolerated) or contain CrowAI's validated `.crowai-private-root` ownership marker, preventing accidental recursive hardening of an unrelated populated directory. Windows permission bits are not treated as a full ACL guarantee; operators must run CrowAI under an appropriately restricted Windows account/service ACL.

Model package directories may be mounted read-only. Runtime-generated model logs, Agent cache/session data and Code debug captures are stored under the dedicated private `instance/model_state/` tree instead of under `models/`; on POSIX its directories/files are hardened to `0700`/`0600`. The model-state root is constrained to the configured private instance directory.

## Model-package trust boundary

Model packages are executable Python plugins. Installing one grants it the privileges of the CrowAI process. Directory and manifest validation prevents accidental traversal and malformed packages; it does **not** sandbox malicious Python. Only install reviewed packages from trusted sources. Optional checksums provide integrity detection, not publisher authenticity. Future isolation may use a separate process or container.

## Upload scope

CrowAI rejects executable formats and suspicious archives, limits count/size/expansion/compression ratio, uses atomic writes, and never exposes stored filesystem paths through public APIs. This is defense in depth, not a substitute for host hardening or malware scanning in high-risk deployments.

## Agent network boundary

Agent V1.0 accepts only HTTP(S) fetch targets, rejects credential-bearing URLs and localhost/private/link-local/reserved destinations by default, resolves hostnames before fetch, validates redirect targets and revalidates the final URL. It respects robots.txt when configured and rate-limits per domain. Fetched/file content is treated as untrusted evidence and prompt-injection-like language is flagged rather than executed as instruction. These controls reduce SSRF risk, but DNS validation and the later socket connection are not cryptographically pinned to one resolution; DNS rebinding/TOCTOU remains a known limitation. Use OS/container/network isolation for hostile environments.

## Code execution boundary

Code V1.0 automatically performs syntax/AST validation without executing generated code. Python execution itself is disabled by default and requires an explicit request policy. The supported isolated backend uses an ephemeral Docker container with `--pull never`, network and IPC disabled, a read-only container root, an unprivileged UID/GID, dropped capabilities, `no-new-privileges`, CPU/memory/PID/open-file/time/output limits, an individual-file size limit, core dumps disabled, no inherited secrets, a read-only host input mount, and a separate writable `/workspace` tmpfs with a hard aggregate size cap. On POSIX the container uses the invoking unprivileged host UID/GID, or UID/GID 65534 when CrowAI itself is root; the host input therefore remains private (`0700`, prepared files `0600`) and generated writes remain inside bounded container memory-backed storage. CrowAI does not silently fall back to host execution when Docker is unavailable. V1.0 ships with an immutable Docker image digest in Code configuration and CI; CrowAI uses `--pull never` during execution and reports whether a configured override remains digest-pinned. The separately named `trusted-local` development backend requires both configuration and environment opt-in, is rejected in production mode, and is a constrained host subprocess **not a security sandbox**; it can access host files/network with the process's permissions. Its Python child ignores Python environment configuration, disables user-site loading and disables `site` initialization (`-E -s -S`) so host `sitecustomize`/global site packages do not make its validation tests depend on unrelated interpreter startup hooks.

## Data deletion

Conversation memory has an SQLite cascade relationship and package cleanup callbacks remove model-local session state. Clear-local-data uses normal owned-conversation deletion before clearing uploads/snapshots. Release gates independently reject runtime databases, upload/user content, cache DBs, logs, secrets, model weights and native runtime binaries.
