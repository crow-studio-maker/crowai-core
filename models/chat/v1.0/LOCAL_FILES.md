# Chat V1.0 local files

Chat V1.0 resolves its inference runtime, model and prompt only below this
`v1.0` directory and does not reuse an external local inference server.

Required private binaries (not stored in this source archive):

- `model/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf`
- `runtime/llama-server.exe` on Windows, plus the matching llama.cpp runtime DLLs
  from the same build in this `runtime/` directory.
- On Linux, an executable `runtime/llama-server` can be used instead of the `.exe` name. Readiness and startup use the same package-local `.exe`/extensionless resolver; no system `PATH` or project-root fallback is used.

Readiness rejects zero-byte/placeholder model files: model/projector files must begin with the `GGUF` magic header. On POSIX, the selected runtime must also have an executable permission bit.
