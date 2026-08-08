# Code V1.0 local files

Code V1.0 resolves its inference runtime, model and prompts only below this
`v1.0` directory and does not reuse an external local inference server.

Required private binaries (not stored in this source archive):

- `model/qwen2.5-coder-7b-instruct-q4_k_m.gguf`
- `runtime/llama-server.exe` on Windows, plus the matching llama.cpp runtime DLLs
  from the same build in this `runtime/` directory.
- On Linux, an executable `runtime/llama-server` can be used instead of the `.exe` name. Readiness and startup use the same package-local `.exe`/extensionless resolver; no system `PATH` or project-root fallback is used.

Readiness rejects zero-byte/placeholder model files: model/projector files must begin with the `GGUF` magic header. On POSIX, the selected runtime must also have an executable permission bit.

## Optional isolated Python execution

Generated Python is not executed by default. When the operator enables the isolated Docker policy, V1.0 defaults to an immutable official-Python `image@sha256:<digest>` reference. CrowAI uses `--pull never`; that exact image must already exist locally. An operator can override the image, but execution evidence reports whether the override remains digest-pinned. Do not substitute a host-process runner for untrusted generated code.

The Docker execution policy additionally applies an individual file-size rlimit (default 8 MiB), disables core dumps, mounts prepared host input read-only at `/input`, and executes from a separate writable `/workspace` tmpfs with a hard aggregate size cap (default 32 MiB). Timeout and temporary-directory cleanup remain additional containment layers.
