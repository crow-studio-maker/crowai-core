# Model development

Start from `examples/model-package-template/`. The template is deliberately non-functional and does not pretend to generate AI answers.

Required callbacks:

```python
def prepare_request(*, question, language, interaction_mode, conversation, attachments, memory_snapshot=None) -> dict:
    ...

def finalize_result(*, question, language, interaction_mode, result) -> dict:
    ...
```

Optional callbacks are `inspect_file()`, `health_check()`, and `shutdown()`. Return JSON-compatible values only. Public results must not contain local filesystem paths, credentials, framework objects, database connections, or arbitrary Python objects.

Attachments passed to `prepare_request()` are sanitized metadata/extracted text, not unrestricted filesystem paths. The optional model `inspect_file()` callback receives a Core-selected file path during upload processing because model packages are trusted code.

Validate before installation:

```bash
python -m tools.validate_model_package /path/to/package
```
