# CrowAI Model Package SDK — Contract v1

CrowAI Core discovers trusted Python packages at `models/<mode>/<version>/`. The official Chat, Code, and Agent V1.0 source packages can be shipped with CrowAI, while their large GGUF models and native runtime binaries may be distributed separately. Third-party packages must be reviewed before installation.

## Required layout

```text
models/
  code/
    mode.json              # optional mode display metadata
    v1.0/
      manifest.json
      __init__.py
      pipeline.py          # optional
      config.json          # optional package-local runtime/model configuration
      model/               # GGUF/model-side files for this version only
      runtime/             # llama.cpp executable + native libraries for this version only
      prompts/             # package-local prompt files
```

Path parts may contain letters, numbers, `.`, `_`, and `-`, and must not contain traversal components. CrowAI's official V1.0 packages resolve configured `*_file` references relative to their own version directory; they do not search the project root or reuse an inference server started by another package.

## Manifest

```json
{
  "id": "v1.0",
  "name": "CrowAI Code",
  "version": "1.0",
  "description": "A project-aware coding model.",
  "display_order": 30,
  "capabilities": ["conversation", "attachments", "code"],
  "model_contract_version": 1,
  "minimum_core_version": "4.2.0",
  "files_sha256": {
    "__init__.py": "optional-64-character-sha256"
  }
}
```

The manifest `id` must match the version directory. The public ID becomes `code/v1.0`. Supported capability names include `conversation`, `attachments`, `file_inspection`, `document_analysis`, `multimodal`, `vision`, `web_search`, `network`, `product_comparison`, `code`, `tools`, `direct_code_generation`, `project_generation`, `project_memory`, `follow_up_editing`, `syntax_validation`, `repair_pass`, `multi_file`, `no_web`, `language_matching`, `safe_python_runner` (legacy compatibility), `python_execution`, `isolated_python_runner`, and `structured_code_task`. Capabilities describe behavior; the Core still decides what services are enabled.

`files_sha256` is optional integrity metadata. It can detect changed package files but is not a signature and does not authenticate a publisher.

## Required callbacks

```python
def prepare_request(
    *,
    question: str,
    language: str,
    interaction_mode: str,
    conversation: list[dict[str, str]],
    attachments: list[dict],
    memory_snapshot: dict | None = None,
) -> dict:
    ...


def finalize_result(
    *,
    question: str,
    language: str,
    interaction_mode: str,
    result: dict,
) -> dict:
    ...
```

`prepare_request()` should return JSON-compatible planning fields such as `query_variations` and `metadata`. `finalize_result()` must return a JSON-compatible object with an `answer` string; optional `sources`, `artifacts`, `warnings`, `analysis`, and metadata are normalized/sanitized by the Core.

## Optional callbacks

- `inspect_file(path, original_name, media_type) -> dict`
- `health_check() -> dict | bool`
- `shutdown() -> None`
- `delete_conversation(conversation_id) -> None` (compatibility cleanup hook)

## Package-local model/runtime rule

When `config.json` contains file settings, use relative paths that stay inside the package. `runtime_file` must live under `runtime/`; `model_file` and `mmproj_file` must live under `model/`. Prompt/provider/site files should also remain package-local. The official V1.0 engines resolve these files from the version directory, launch their own loopback-only inference process, verify the advertised model alias, and never attach to a foreign local llama-server process. Runtime readiness and startup share one resolver: a configured `.exe` may use an extensionless sibling on Linux and an extensionless configuration may use the `.exe` sibling on Windows, but candidates must remain inside the package. On POSIX the runtime must be executable. GGUF model/projector readiness rejects empty or non-`GGUF` placeholder files. Public readiness reports safe requirement categories rather than filesystem paths.

Uploaded files are request inputs rather than model/runtime dependencies. Core may pass a trusted transient upload path privately to a package so it can inspect the user's file; that private path is stripped from public results.

## Security boundary

A model package runs inside the CrowAI Python process and is trusted code. The Core does not pass Flask request/session objects, database connections, password hashes, API keys, SMTP credentials, private environment variables, or unrestricted filesystem roots into normal request callbacks. Public results are stripped of private/local paths while safe relative artifact paths such as `src/main.py` are preserved.

Install only reviewed packages. Contract v1 does not sandbox Python package code, so package review and package-local path validation remain important.

## Validation

```bash
python -m tools.validate_model_package models/chat/v1.0
python -m tools.validate_model_package models/code/v1.0
python -m tools.validate_model_package models/agent/v1.0
```

The validator checks path names, manifest fields, contract/core versions, callbacks in source, optional checksums, forbidden runtime/private files, and package-local runtime/model config paths. It does not execute model code by default.
