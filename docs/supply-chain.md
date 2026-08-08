# V1.0 supply-chain baseline

CrowAI V1.0 keeps release artifacts deterministic and reduces external dependency drift without inventing upstream identifiers.

The runtime and development requirement files exact-pin CrowAI's direct Python dependencies. The PEP 517 build backend is pinned to `setuptools==83.0.0`, and CI pins the pip bootstrap to `26.2.1`. GitHub Actions are referenced by full upstream commit SHA, with comments recording the reviewed release tag: `actions/checkout` v4.4.0 at `11d5960a326750d5838078e36cf38b85af677262` and `actions/setup-python` v5.6.0 at `a26af69be951a213d495a4c3e4e4022e16d87065`. Code's isolated Python runner and its CI test use the official `python:3.13-slim` image at index digest `sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6` with runtime pull policy `never`.

These pins were verified against the upstream GitHub release/commit pages, Docker Official Image metadata, and PyPI on 2026-08-08. CI uses the explicit `ubuntu-24.04` runner generation instead of the rolling `ubuntu-latest` alias. They should be deliberately reviewed before changing them.

This is not a claim that Python dependency installation is fully hermetic on every platform. Transitive wheels and sdists are not yet locked with per-artifact hashes for every supported OS/architecture. A future supply-chain-only maintenance change can add generated, reviewed, hash-locked platform lock files without changing CrowAI V1.0 behavior.
