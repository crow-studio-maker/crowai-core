# Contributing

## Development setup

Create a virtual environment, install `requirements-dev.txt`, copy `.env.example` to `.env`, and run `pytest` before making changes.

## Boundaries

- Do not commit secrets, runtime databases, sessions, user JSON, uploads, logs, model weights, or private prompts.
- Do not add fake AI model implementations to the Core.
- Keep SQL in repositories and business/ownership rules in services.
- Preserve public URLs or provide explicit compatibility redirects.
- Treat model packages as trusted executable Python extensions.
- Do not modify the Settings markup, labels, fields, `settings_json` semantics, or `/api/settings` contract without a documented security/data-loss fix and regression test.

## Quality gate

```bash
python tools/check_format.py
ruff check .
pytest --cov=crowai --cov=models
python tools/build_release.py
```

## Pull-request checklist

- Tests cover the behavior and security boundary.
- No local path, credential, token, private prompt, or user content appears in fixtures.
- No runtime/private file is added.
- Error responses are safe and include a request ID.
- Model callbacks receive only documented data.
- Documentation and release notes are updated when behavior changes.

Use focused commits with imperative messages. Security reports must follow `SECURITY.md`, not a public issue.
