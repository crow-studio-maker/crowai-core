# Agent V1.0 local files

Agent V1.0 is deliberately self-contained. Its inference runtime, model weights,
projector and prompts are resolved only below this `v1.0` directory. It never
reuses an already-running local inference server.

Required private binaries (not stored in this source archive):

- `model/Qwen3VL-8B-Instruct-Q4_K_M.gguf`
- `model/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf`
- `runtime/llama-server.exe` on Windows, plus the matching llama.cpp runtime DLLs
  from the same build in this `runtime/` directory.
- On Linux, an executable `runtime/llama-server` can be used instead of the `.exe` name. Readiness and startup use the same package-local `.exe`/extensionless resolver; no system `PATH` or project-root fallback is used.

Uploaded user files are transient inputs owned by CrowAI Core. Agent receives a
private one-turn path only to inspect the uploaded bytes; that path is never a
model/runtime dependency and is removed from stored/public metadata.

Readiness rejects zero-byte/placeholder model files: model/projector files must begin with the `GGUF` magic header. On POSIX, the selected runtime must also have an executable permission bit.
