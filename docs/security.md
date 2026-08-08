# Security design

CrowAI applies layered controls: validated startup configuration, opaque server-side sessions, CSRF and origin checks, owner-scoped repositories/services, safe upload storage, archive limits, recursive path sanitization, structured safe errors, CSP and related browser headers, and release-time privacy scanning.

Logs may contain request IDs, route, method, status, duration, opaque owner type/user ID, and safe error codes. They must never contain passwords, session contents, CSRF tokens, upload bodies, private prompts, API keys, or raw model exceptions returned to users.

Model plugins remain the largest trust boundary because they execute as Python code. Run only reviewed packages and use process/container isolation where adversarial packages are possible.

## Local private filesystem permissions

On POSIX, CrowAI explicitly hardens configured private runtime roots (`instance/`, `users/`, `uploads/` and per-owner upload directories) to `0700`, and private files such as `workspace.db`, SQLite WAL/SHM sidecars, uploads, upload `.part` files, `secret.key` and user JSON snapshots to `0600`. If the configured database is nested below `instance/`, every CrowAI-owned intermediate directory is also forced to `0700`; strict/production mode rejects a database path outside the configured private instance root. User-configured runtime paths are checked lexically before canonical resolution, so symlink/junction/reparse-point components cannot redirect recursive permission changes. Custom production roots are recursively hardened only when they are new/empty (apart from `.gitkeep`) or already carry CrowAI's validated `.crowai-private-root` ownership marker. Root symlinks passed directly to the private permission helpers are rejected rather than followed, while child symlinks encountered during tree traversal are skipped/rejected according to strictness. Existing broader modes are tightened at startup instead of relying on the process umask. Production fails closed when these POSIX permissions cannot be enforced; development emits a clear warning on filesystems that do not support the operation. Windows `chmod` handling is best-effort only—Windows ACL configuration remains an operator/deployment responsibility.

Model package directories may be mounted read-only. Runtime-generated model logs, Agent cache/session data and Code debug captures are stored under the dedicated private `instance/model_state/` tree instead of under `models/`; on POSIX its directories/files are hardened to `0700`/`0600`. The model-state root is constrained to the configured private instance directory.

## Code execution trust boundary

Code V1.0 syntax/AST validation does not execute generated code. Actual Python execution is disabled by default and requires an explicit request-level policy. The isolated backend is an ephemeral Docker container with network disabled, a read-only container root, dropped capabilities, no-new-privileges, CPU/memory/PID/output/time/open-file limits, per-file `RLIMIT_FSIZE`, core dumps disabled, no inherited secrets with a read-only host input mount and a separate writable `/workspace` tmpfs. On POSIX, CrowAI mirrors an unprivileged host UID/GID into the container, while a root-launched CrowAI process hands the private input tree to UID/GID 65534; host directories remain `0700` and prepared files `0600`. `/workspace` has a hard aggregate tmpfs size cap, so generated code cannot grow a bind-mounted host workspace. If that backend is unavailable, CrowAI does not fall back to host execution. `trusted-local` is a development-only double opt-in constrained subprocess, not a sandbox, and can access host resources with the CrowAI process permissions. Its child interpreter uses `-E -s -S`, avoiding inherited Python environment configuration, user-site packages, global `sitecustomize` startup hooks, and unrelated site-package initialization while retaining imports from the generated workspace.

## Agent network limitation

Agent blocks obvious local/private destinations, validates DNS results before fetch, and revalidates redirects/final URLs. The validation result is not cryptographically pinned to the later socket connection, so DNS rebinding/TOCTOU remains a known limitation. Treat fetched content as untrusted and prefer host/container/network isolation for hostile deployments.

## Code execution image reproducibility

The isolated Code runner uses Docker with `--pull never`; the image must already be present locally. The V1.0 Code configuration and CI use the official Python image through an operator-verified `image@sha256:<digest>` reference. CrowAI reports whether an operator override remains digest-pinned in execution evidence and never pulls an image during a model request.
